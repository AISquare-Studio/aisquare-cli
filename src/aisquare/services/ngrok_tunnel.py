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

import json
import os
import re
import shutil
import subprocess
import threading
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

_TOO_OLD_FOR_URL = re.compile(r"(?:unknown flag|flag provided but not defined):\s*-{1,2}url\b")
"""What ngrok v3 (``unknown flag: --url``) and v2 (``flag provided but not defined: -url``)
print as plain text on stderr, before any JSON log, when they do not know ``--url``."""


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


def parse_log_line(line: str) -> LogEvent:
    """What one log line means for us; an irrelevant line means nothing.

    The lines are JSON, but for one: an ngrok too old for ``--url`` says so in
    plain text before it logs anything, and that becomes :data:`TOO_OLD_HINT`.
    """
    try:
        record: Any = json.loads(line)
    except ValueError:
        return LogEvent(error=TOO_OLD_HINT) if _TOO_OLD_FOR_URL.search(line) else LogEvent()
    if not isinstance(record, dict):
        return LogEvent()
    if record.get("msg") == "started tunnel" and isinstance(record.get("url"), str):
        return LogEvent(url=record["url"])
    if record.get("lvl") in ("eror", "error", "crit"):
        err = record.get("err") or record.get("msg") or "ngrok reported an error"
        text = str(err)
        if "authtoken" in text.lower() or "ERR_NGROK_4018" in text:
            text = AUTHTOKEN_HINT
        elif _TOO_OLD_FOR_URL.search(text):
            text = TOO_OLD_HINT
        return LogEvent(error=text)
    return LogEvent()


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
        self._reader: threading.Thread | None = None
        self._lock = threading.Lock()
        self._url_ready = threading.Event()
        self.public_url: str | None = None
        self.error: str | None = None
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
            self._process = self._popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            self.error = f"could not start {command[0]}: {exc}"
            return self.error
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
        process = self._process
        self._process = None
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if process.stdout is not None:
            process.stdout.close()

    # --- the log reader ---------------------------------------------------------------

    def _read_log(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            for line in process.stdout:
                self.handle_line(line)
        except ValueError:  # the pipe was closed under us by stop_tunnel()
            return
        if self.public_url is None and self.error is None:
            code = process.poll()
            self.error = f"ngrok exited (code {code}) before it announced a tunnel"
        self._url_ready.set()  # a URL that never comes must not block a waiter forever

    def handle_line(self, line: str) -> None:
        event = parse_log_line(line)
        with self._lock:
            if event.url is not None:
                self.public_url = event.url
                self._url_ready.set()
            elif event.error is not None and self.error is None:
                self.error = event.error
