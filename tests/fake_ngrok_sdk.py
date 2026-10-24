"""A stand-in for the ngrok SDK (``import ngrok``), so no test reaches ngrok's service (#34).

``conftest.no_real_ngrok`` puts one in ``sys.modules`` for every test, refusing every sign-in
and recording it; a test that wants a tunnel or a token check makes its own with
:func:`fake_sdk` and puts it there instead. The calls the bundled tunnel makes, and only
those: ``SessionBuilder().authtoken(t).handle_disconnection(h).connect()``,
``session.http_endpoint().domain(d).listen_and_forward(url)``, ``listener.url()``,
``session.close()``.
"""

from __future__ import annotations

import asyncio
import threading
import types
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


class SdkError(ValueError):
    """What the SDK raises: ``ValueError(message, cause, code)``."""


@dataclass
class Calls:
    """What the SDK was asked, in order."""

    tokens: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)
    forwards: list[str] = field(default_factory=list)
    closed: int = 0
    cancelled: int = 0
    """Sign-ins given up while :attr:`Script.hold` held them (the SDK's socket closed)."""


@dataclass
class Script:
    """How the fake answers: ``refuse`` an ngrok code for every sign-in (``ERR_NGROK_105``),
    else a tunnel at ``url``. ``hold``, while set, keeps every sign-in waiting until it is
    released (``hold.set()`` lets them go); ``signing_in`` is set once one waits so."""

    url: str = "https://abc123.ngrok-free.app"
    refuse: str | None = None
    hold: threading.Event | None = None
    signing_in: threading.Event = field(default_factory=threading.Event)
    calls: Calls = field(default_factory=Calls)
    on_disconnect: list[Callable[[str, str], None]] = field(default_factory=list)
    """Every session's disconnection handler, as the builder was handed it."""

    def disconnect(self, said: str, addr: str = "connect.ngrok-agent.com:443") -> None:
        """What the SDK does as ngrok's service lets the last session go: its handler, if it
        was handed one, is told, on a thread that is not the tunnel's; with none, the SDK
        only signs in again, and nobody hears of it."""
        for handler in self.on_disconnect[-1:]:
            told = threading.Thread(target=handler, args=(addr, said))
            told.start()
            told.join()


class _Listener:
    def __init__(self, url: str) -> None:
        self._url = url

    def url(self) -> str:
        return self._url


class _Endpoint:
    def __init__(self, script: Script) -> None:
        self._script = script

    def domain(self, domain: str) -> _Endpoint:
        self._script.calls.domains.append(domain)
        return self

    async def listen_and_forward(self, url: str) -> _Listener:
        self._script.calls.forwards.append(url)
        return _Listener(self._script.url)


class _Session:
    def __init__(self, script: Script) -> None:
        self._script = script

    def http_endpoint(self) -> _Endpoint:
        return _Endpoint(self._script)

    async def close(self) -> None:
        self._script.calls.closed += 1


class _Builder:
    def __init__(self, script: Script) -> None:
        self._script = script
        self._token = ""

    def authtoken(self, token: str) -> _Builder:
        self._token = token
        return self

    def handle_disconnection(self, handler: Callable[[str, str], None]) -> _Builder:
        self._script.on_disconnect.append(handler)
        return self

    async def connect(self) -> _Session:
        script = self._script
        script.calls.tokens.append(self._token)
        hold = script.hold
        if hold is not None:
            script.signing_in.set()
            try:
                while not hold.is_set():
                    await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                script.calls.cancelled += 1
                raise
        if script.refuse is not None:
            raise SdkError("failed to connect session", "the authtoken was refused", script.refuse)
        return _Session(script)


def fake_sdk(script: Script) -> Any:
    """A module standing in for ``ngrok``, answering as ``script`` says."""
    module = types.ModuleType("ngrok")
    module.SessionBuilder = lambda: _Builder(script)  # type: ignore[attr-defined]
    return module
