"""ngrok, bundled: the official ngrok Python SDK, in this process (#34).

Remote reached a phone through an ngrok agent the human installed and signed in themselves
(``ngrok_tunnel``: "the binary is the human's job"), and a fresh install answered Remote with
"ngrok is not installed". The owner (2026-10-23): ngrok is part of the CLI, and its token is
asked for in Settings. So the agent is the ``ngrok`` package on PyPI, a core dependency: a
wheel for every platform the CLI supports, which ``asq upgrade`` brings along, nothing
downloaded at run time.

The SDK embeds the agent (ngrok-rust) and has neither a traffic inspector nor a local agent
API: nothing listens on ``127.0.0.1:4040`` for any user of the machine to read or drive, which
``ngrok_tunnel`` works hard to switch off in the binary. The tunnel lives on a thread of this
process, so it ends with it, however that ends.

**The token** (:func:`find_authtoken`), first found wins: ``NGROK_AUTHTOKEN``; the one pasted
in Settings → Remote, kept in the aisquare home, owner-only (:func:`save_token`); the human's
own ngrok config (``authtoken:`` top-level in a version-2 file, under ``agent:`` in a
version-3 one, where ``ngrok config add-authtoken`` writes it), so someone who already had
ngrok signed in needs nothing new. The token is never said: not in a toast, a log or an error.

Only an install the SDK cannot be imported from (a damaged one) falls back to an ``ngrok`` on
PATH (:func:`bundled_ngrok`).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

from aisquare.core import paths
from aisquare.core.atomic import write_replacing
from aisquare.core.config import decode_by_bom
from aisquare.services import ngrok_tunnel
from aisquare.services.ngrok_tunnel import (
    AUTHTOKEN_FROM,
    AUTHTOKEN_HINT,
    NGROK_URL_ENV,
    STOP_SECONDS,
    UPSTREAM_HOST,
    NgrokTunnel,
    Tunnel,
    _one_line,
    ngrok_static_host,
)

log = logging.getLogger(__name__)

TOKEN_FILE_NAME = "ngrok-authtoken"
"""The token pasted in Settings → Remote, in the aisquare home, owner-only."""
BAD_TOKEN = (
    "ngrok did not take your token — paste a new one in Settings → Remote "
    f"(yours: {AUTHTOKEN_FROM})"
)
"""What Remote says when ngrok's service refused the token it was given."""
REFUSED_TOKEN = "ngrok did not take that token: copy it again from your ngrok dashboard"
"""What the line beside the token field says of a token ngrok refused (:func:`check_token`)."""
TOKEN_CODES = frozenset({"ERR_NGROK_105", "ERR_NGROK_106", "ERR_NGROK_107", "ERR_NGROK_4018"})
"""ngrok's error codes for a token it will not sign in with: malformed (105), revoked or
reset (106, 107), none or not a verified account's (4018)."""
CONNECT_SECONDS = 20.0
"""How long a sign-in to ngrok's service may take before the start says why there is none:
the watchdog starts it again each minute (``RemoteController.revive_tunnel_if_dead``)."""
CHECK_SECONDS = 10.0
"""How long Settings' *Save* waits for ngrok to say whether it takes a token."""
SDK_MISSING = "ngrok is missing from this install, so a phone cannot reach Remote yet"
"""Why there is no tunnel in an install the SDK cannot be imported from (a damaged one)."""


# --- the SDK ------------------------------------------------------------------------------


def sdk() -> ModuleType | None:
    """The ngrok SDK, or ``None`` in an install it cannot be imported from."""
    try:
        import ngrok
    except Exception:  # a damaged install: an ImportError, or a native library that fails
        return None
    return ngrok


def error_code(exc: BaseException) -> str | None:
    """The ``ERR_NGROK_<n>`` an SDK error carries (its last argument), if any."""
    for arg in reversed(exc.args):
        if isinstance(arg, str) and arg.startswith("ERR_NGROK_"):
            return arg
    return None


def failure_sentence(exc: BaseException, token: str) -> str:
    """What the status line says of an SDK error: :data:`BAD_TOKEN` for a token ngrok will not
    take, else ngrok's own words, on one line, the token cut out of them."""
    code = error_code(exc)
    if code in TOKEN_CODES:
        return BAD_TOKEN
    if isinstance(exc, TimeoutError):
        return "ngrok could not reach its service — Remote tries again within a minute"
    words = [str(arg) for arg in exc.args if isinstance(arg, str) and arg != code]
    said = (
        _one_line(" — ".join(words).replace(token, "…")) if token else _one_line(" — ".join(words))
    )
    said = said or type(exc).__name__
    return f"ngrok: {said}" + (f" ({code})" if code else "")


# --- the token ----------------------------------------------------------------------------


def token_path() -> Path:
    return paths.aisquare_home() / TOKEN_FILE_NAME


def saved_token() -> str | None:
    """The token pasted in Settings, or ``None``."""
    try:
        token = token_path().read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return token or None


def save_token(token: str) -> None:
    """Keep ``token`` in the aisquare home, owner-only (0600), in one step."""
    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_replacing(path, token.strip() + "\n", owner_only=True)


def ngrok2_config() -> Path:
    """Where ngrok 2 kept its config, and its token."""
    return Path.home() / ".ngrok2" / "ngrok.yml"


def their_configs() -> list[Path]:
    """Where the human's own ngrok keeps its token: ngrok 3's config, and ngrok 2's."""
    return [ngrok_tunnel.ngrok_default_config(), ngrok2_config()]


def token_in_config(path: Path) -> str | None:
    """The authtoken ngrok's config at ``path`` signs in with: ``authtoken:`` at the top (a
    version-2 file) or under ``agent:`` (version 3); ``None`` for none, or no readable file."""
    import yaml

    try:
        data: Any = yaml.safe_load(decode_by_bom(path.read_bytes(), errors="replace"))
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    agent = data.get("agent")
    for found in (
        agent.get("authtoken") if isinstance(agent, dict) else None,
        data.get("authtoken"),
    ):
        if isinstance(found, str) and found.strip():
            return found.strip()
    return None


def find_authtoken(
    environ: Mapping[str, str] = os.environ, configs: list[Path] | None = None
) -> str | None:
    """The token Remote's ngrok signs in with: ``NGROK_AUTHTOKEN``, the one pasted in Settings,
    or the human's own ngrok config's (``configs``, :func:`their_configs` when not given)."""
    token = environ.get("NGROK_AUTHTOKEN", "").strip() or saved_token()
    if token:
        return token
    for config in their_configs() if configs is None else configs:
        token = token_in_config(config)
        if token:
            return token
    return None


def check_token(token: str, *, module: ModuleType | None = None) -> str | None:
    """Whether ngrok's service takes ``token``: ``None`` when it does, or when that cannot be
    told now (no network, the SDK missing, the account's one session in use elsewhere), else
    :data:`REFUSED_TOKEN`. A sign-in, closed at once; no tunnel is opened."""
    module = sdk() if module is None else module
    if module is None:
        return None

    async def sign_in() -> None:
        session = await asyncio.wait_for(
            module.SessionBuilder().authtoken(token).connect(), CHECK_SECONDS
        )
        with contextlib.suppress(Exception):
            await session.close()

    try:
        asyncio.run(sign_in())
    except Exception as exc:
        if error_code(exc) in TOKEN_CODES:
            return REFUSED_TOKEN
        log.info("ngrok: the token could not be checked: %s", type(exc).__name__)
    return None


# --- the tunnel ---------------------------------------------------------------------------


class SdkTunnel:
    """Remote's tunnel through the bundled SDK, as :class:`~ngrok_tunnel.NgrokTunnel` is one
    through the binary, to the controller alike (:class:`~ngrok_tunnel.Tunnel`).

    :meth:`start_tunnel` signs in and opens the tunnel on a thread of its own, which runs an
    event loop for the SDK until :meth:`stop_tunnel`; the URL is announced from there.
    ``module`` and ``token`` are seams: tests hand a fake SDK.
    """

    api_warning: str | None = None
    """The SDK has no local agent API, so there is never one to warn of."""

    def __init__(
        self,
        port: int,
        *,
        module: Callable[[], ModuleType | None] = sdk,
        token: Callable[[], str | None] = find_authtoken,
    ) -> None:
        self.port = port
        self._module = module
        self._token = token
        self.public_url: str | None = None
        self.error: str | None = None
        self.on_announce: Callable[[str], None] | None = None
        self.static_host = ngrok_static_host(os.environ.get(NGROK_URL_ENV))
        self._lock = threading.Lock()
        self._url_ready = threading.Event()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None

    def start_tunnel(self) -> str | None:
        """Start signing in; ``None`` once that is under way, else the sentence to show."""
        module = self._module()
        if module is None:
            self.error = SDK_MISSING
            return self.error
        token = self._token()
        if not token:
            self.error = AUTHTOKEN_HINT
            return self.error
        self._thread = threading.Thread(
            target=self._remote_tunnel_thread, args=(module, token), name="ngrok-sdk", daemon=True
        )
        self._thread.start()
        return None

    def _remote_tunnel_thread(self, module: ModuleType, token: str) -> None:
        try:
            asyncio.run(self._remote_tunnel_serve(module, token))
        except Exception as exc:
            with self._lock:
                if self.error is None and not self._stopping.is_set():
                    self.error = failure_sentence(exc, token)
        finally:
            self._url_ready.set()  # a waiter hears now that there will be no URL

    async def _remote_tunnel_serve(self, module: ModuleType, token: str) -> None:
        self._loop, self._stop = asyncio.get_running_loop(), asyncio.Event()
        if self._stopping.is_set():
            return
        session = await asyncio.wait_for(
            module.SessionBuilder().authtoken(token).connect(), CONNECT_SECONDS
        )
        try:
            endpoint = session.http_endpoint()
            if self.static_host:
                endpoint = endpoint.domain(self.static_host)
            listener = await endpoint.listen_and_forward(f"http://{UPSTREAM_HOST}:{self.port}")
            if self._stopping.is_set():
                return
            self._announce(str(listener.url()))
            await self._stop.wait()
        finally:
            with contextlib.suppress(Exception):
                await session.close()

    def _announce(self, url: str) -> None:
        with self._lock:
            self.public_url = url
        self._url_ready.set()
        announced = self.on_announce
        if announced is not None:
            try:
                announced(url)
            except Exception:
                log.warning("ngrok: the announced URL could not be taken", exc_info=True)

    def wait_for_url(self, timeout: float = 15.0) -> str | None:
        """Block up to ``timeout`` seconds for the public URL; ``None`` when it did not arrive."""
        self._url_ready.wait(timeout)
        return self.public_url

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def stop_tunnel(self) -> None:
        """Close the tunnel and sign out, waiting up to :data:`~ngrok_tunnel.STOP_SECONDS`."""
        self._stopping.set()
        loop, stop = self._loop, self._stop
        if loop is not None and stop is not None:
            with contextlib.suppress(RuntimeError):  # its loop has ended already
                loop.call_soon_threadsafe(stop.set)
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(STOP_SECONDS)

    def signal_now(self) -> None:
        """Nothing to signal: the tunnel is this process's, and ends with it."""


def bundled_ngrok(port: int) -> Tunnel:
    """Remote's tunnel for ``port``: through the bundled SDK, or, in an install the SDK cannot be
    imported from, an ``ngrok`` on PATH with its agent API off (``ngrok_tunnel``)."""
    if sdk() is not None:
        return SdkTunnel(port)
    return NgrokTunnel(port, api_off=True)


def saved_token_environment(environ: Mapping[str, str] = os.environ) -> dict[str, str] | None:
    """The environment an ``ngrok`` binary starts with so that it signs in with the token
    pasted in Settings, where none is exported; ``None`` to start it with this process's."""
    if environ.get("NGROK_AUTHTOKEN", "").strip():
        return None
    token = saved_token()
    return None if token is None else {**environ, "NGROK_AUTHTOKEN": token}
