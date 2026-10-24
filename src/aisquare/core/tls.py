"""The one verified TLS context every HTTPS call in this package uses.

WHY ONE. ``urllib.request.urlopen`` with no ``context`` verifies against
whatever CA bundle the interpreter's OpenSSL was built to find. Linux distro
Pythons find ``/etc/ssl``; a python.org framework build on macOS points at a
``cert.pem`` that does not exist until someone runs its "Install
Certificates.command" — so every request failed ``CERTIFICATE_VERIFY_FAILED``
and the usage rows said "could not reach the usage endpoint (URLError)" while
``curl`` to the same URL worked, because curl asks the system trust store.

ORDER, first that applies:

1. ``SSL_CERT_FILE`` / ``SSL_CERT_DIR`` set: the operator named a bundle, so it
   is the stdlib context over exactly that (a proxy that re-signs HTTPS).
2. ``truststore``: the OS trust store — the macOS Keychain, Windows' store, the
   distro bundle on Linux — so a corporate CA the machine trusts is trusted here.
3. ``certifi``: Mozilla's bundle, for an interpreter ``truststore`` cannot use.
4. The stdlib default, which is what every call had before.

LAZY. ``ssl`` stays out of the base CLI's import closure
(tests/test_import_cost_of_the_integration.py), so it, ``truststore`` and
``certifi`` are imported inside :func:`ssl_context`, built once per process.
"""

from __future__ import annotations

import functools
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import ssl

#: What :func:`trust_source` names for each rung of the order above.
SOURCE_ENV = "SSL_CERT_FILE/SSL_CERT_DIR"
SOURCE_TRUSTSTORE = "the OS trust store (truststore)"
SOURCE_CERTIFI = "certifi's CA bundle"
SOURCE_DEFAULT = "the interpreter's default CA paths"

#: What to do about a certificate that did not verify: doctor's fix line.
CERT_FIX = "set SSL_CERT_FILE to your CA bundle if a proxy re-signs HTTPS"
#: The hint a certificate failure carries, short enough for a usage row.
CERT_HINT = f"{CERT_FIX}, or see `aisquare doctor --live`"


def _build() -> tuple[ssl.SSLContext, str]:
    import ssl

    cafile = os.environ.get("SSL_CERT_FILE") or None
    capath = os.environ.get("SSL_CERT_DIR") or None
    if cafile or capath:
        return ssl.create_default_context(cafile=cafile, capath=capath), SOURCE_ENV
    try:
        import truststore
    except ImportError:
        pass
    else:
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT), SOURCE_TRUSTSTORE
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context(), SOURCE_DEFAULT
    return ssl.create_default_context(cafile=certifi.where()), SOURCE_CERTIFI


@functools.cache
def _cached() -> tuple[ssl.SSLContext, str]:
    return _build()


def ssl_context() -> ssl.SSLContext:
    """The verified client context: hostname checked, certificate required."""
    return _cached()[0]


def trust_source() -> str:
    """Which rung of the order the context came from, for ``doctor``."""
    return _cached()[1]


def is_https(request: Any) -> bool:
    """Whether ``request`` (a URL or a ``urllib.request.Request``) is an ``https://`` one."""
    url = request if isinstance(request, str) else getattr(request, "full_url", "")
    return str(url).lower().startswith("https:")


def urlopen(request: Any, *, timeout: float) -> Any:
    """``urllib.request.urlopen`` with :func:`ssl_context`: the way this package opens a URL.

    The context goes to an ``https://`` request only: built for a plain ``http://`` one
    (the explainability proxy's ``/health`` on loopback), an ``SSL_CERT_FILE`` that will
    not load failed a call that never speaks TLS (review of #403, M2).

    Looked up on ``urllib.request`` at call time, so a test that replaces
    ``urllib.request.urlopen`` still intercepts it.
    """
    import urllib.request

    if not is_https(request):
        return urllib.request.urlopen(request, timeout=timeout)
    return urllib.request.urlopen(request, timeout=timeout, context=ssl_context())


def https_handler() -> Any:
    """A ``urllib.request.HTTPSHandler`` for ``build_opener`` that verifies through
    :func:`ssl_context`, built at its first ``https://`` request rather than here: an
    opener that also carries ``http://`` requests is not broken by a bundle that will not
    load, and building the opener costs no context."""
    import http.client
    import urllib.request

    class _Verified(urllib.request.HTTPSHandler):
        def https_open(self, req: Any) -> Any:
            return self.do_open(http.client.HTTPSConnection, req, context=ssl_context())

    return _Verified()


def cert_failure(exc: BaseException) -> bool:
    """Whether ``exc`` (bare, or wrapped in a ``URLError``) is a certificate that did not verify."""
    import ssl

    reason = getattr(exc, "reason", None)
    return isinstance(exc, ssl.SSLCertVerificationError) or isinstance(
        reason, ssl.SSLCertVerificationError
    )


def https_proxy(host: str) -> str | None:
    """The ``HTTPS_PROXY`` a request to ``host`` goes through, as ``urlopen`` picks it
    (``NO_PROXY`` honoured), or ``None`` for a direct connection."""
    import urllib.request

    proxy = urllib.request.getproxies().get("https")
    if not proxy or urllib.request.proxy_bypass(host):
        return None
    return str(proxy)


def handshake(host: str, *, port: int = 443, timeout: float = 10.0) -> None:
    """One verified TLS handshake to ``host``, nothing sent: raises as ``urlopen`` would.

    Through ``HTTPS_PROXY`` when one applies, by ``CONNECT``, as every request of this
    package goes: direct, a machine that may only leave through its proxy failed here
    while its usage reads worked (review of #403).
    """
    import base64
    import http.client
    from urllib.parse import unquote, urlsplit

    proxy = https_proxy(host)
    if proxy is None:
        connection = http.client.HTTPSConnection(host, port, timeout=timeout, context=ssl_context())
    else:
        parts = urlsplit(proxy if "://" in proxy else f"http://{proxy}")
        connection = http.client.HTTPSConnection(
            parts.hostname or "", parts.port or 80, timeout=timeout, context=ssl_context()
        )
        headers = {}
        if parts.username:
            secret = f"{unquote(parts.username)}:{unquote(parts.password or '')}"
            headers["Proxy-Authorization"] = "Basic " + base64.b64encode(secret.encode()).decode()
        connection.set_tunnel(host, port, headers=headers)
    try:
        connection.connect()
    finally:
        connection.close()
