"""A stand-in for the ngrok SDK (``import ngrok``), so no test reaches ngrok's service (#34).

``conftest.no_real_ngrok`` puts one in ``sys.modules`` for every test, refusing every sign-in
and recording it; a test that wants a tunnel or a token check makes its own with
:func:`fake_sdk` and puts it there instead. The calls the bundled tunnel makes, and only
those: ``SessionBuilder().authtoken(t).connect()``, ``session.http_endpoint().domain(d)
.listen_and_forward(url)``, ``listener.url()``, ``session.close()``.
"""

from __future__ import annotations

import types
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


@dataclass
class Script:
    """How the fake answers: ``refuse`` an ngrok code for every sign-in (``ERR_NGROK_105``),
    else a tunnel at ``url``."""

    url: str = "https://abc123.ngrok-free.app"
    refuse: str | None = None
    calls: Calls = field(default_factory=Calls)


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

    async def connect(self) -> _Session:
        self._script.calls.tokens.append(self._token)
        if self._script.refuse is not None:
            raise SdkError(
                "failed to connect session", "the authtoken was refused", self._script.refuse
            )
        return _Session(self._script)


def fake_sdk(script: Script) -> Any:
    """A module standing in for ``ngrok``, answering as ``script`` says."""
    module = types.ModuleType("ngrok")
    module.SessionBuilder = lambda: _Builder(script)  # type: ignore[attr-defined]
    return module
