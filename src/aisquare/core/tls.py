"""The one verified TLS context every HTTPS call in this package uses.

WHY ONE. ``urllib.request.urlopen`` with no ``context`` verifies against
whatever CA bundle the interpreter's OpenSSL was built to find. Linux distro
Pythons find ``/etc/ssl``; a python.org framework build on macOS points at a
``cert.pem`` that does not exist until someone runs its "Install
Certificates.command" — so every request failed ``CERTIFICATE_VERIFY_FAILED``
and the usage rows said "could not reach the usage endpoint (URLError)" while
``curl`` to the same URL worked, because curl asks the system trust store.

ORDER, first that applies:

1. ``SSL_CERT_FILE`` / ``SSL_CERT_DIR`` set: the operator named a bundle (a proxy that
   re-signs HTTPS). It is the stdlib's context as it was before this module, and
   trusts what that trusted: OpenSSL's default paths are those two variables when
   set, so ``SSL_CERT_FILE`` REPLACES the default CA file (only the default
   directory stays) and ``SSL_CERT_DIR`` replaces the default directory (the default
   file stays). A bundle in ``SSL_CERT_FILE`` must hold the public roots too, then;
   a hashed ``SSL_CERT_DIR`` adds to them. On Windows both add to the Windows store,
   which the stdlib loads first. No wider than OpenSSL and Python make it: trust is
   not silently widened (review of #401, m3). The bundle is loaded explicitly as
   well, so one that will not load says so.
2. ``truststore``: the OS trust store — the macOS Keychain, Windows' store, the
   distro bundle on Linux — so a corporate CA the machine trusts is trusted here.
3. ``certifi``: Mozilla's bundle, for an interpreter that cannot import ``truststore``.
   Only then: ``truststore`` imports on every supported Python, and a context it builds
   over a machine with no store at all (a minimal container with no CA files) fails to
   verify rather than falling back here (n3); ``SSL_CERT_FILE=$(python -m certifi)``
   is the way to name certifi's bundle there.
4. The stdlib default, which is what every call had before.

Every rung requires a certificate, checks the hostname and refuses below TLS 1.2, set
here rather than left to the interpreter's defaults (n9).

LAZY. ``ssl`` stays out of the base CLI's import closure
(tests/test_import_cost_of_the_integration.py), so it, ``truststore`` and
``certifi`` are imported inside :func:`ssl_context`, built once per THREAD: truststore
holds a context's lock across a whole handshake, so one context shared by a process ran
every handshake one at a time, and one that stalled held up every other (m1).
"""

from __future__ import annotations

import os
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import ssl

#: What :func:`trust_source` names for each rung of the order above.
SOURCE_ENV = "SSL_CERT_FILE/SSL_CERT_DIR"
SOURCE_TRUSTSTORE = "the OS trust store (truststore)"
SOURCE_CERTIFI = "certifi's CA bundle"
SOURCE_DEFAULT = "the interpreter's default CA paths"

#: What to do about a certificate that did not verify: doctor's fix line.
#: SSL_CERT_FILE replaces the default CA file, so the bundle it names holds both (m3).
CERT_FIX = "if a proxy re-signs HTTPS, set SSL_CERT_FILE to a bundle of its CA and the public roots"
#: The hint a certificate failure carries, short enough for a usage row.
CERT_HINT = f"{CERT_FIX}, or see `aisquare doctor --live`"
#: What to do about a bundle that will not load: doctor's fix line, and a usage row's hint.
BUNDLE_FIX = "point SSL_CERT_FILE / SSL_CERT_DIR at a readable PEM bundle, or unset them"
#: What to do about an ``HTTPS_PROXY`` that names no host and port this can dial.
PROXY_FIX = "set HTTPS_PROXY to scheme://host:port (and NO_PROXY for hosts it must not proxy)"


class CABundleError(OSError):
    """``SSL_CERT_FILE`` / ``SSL_CERT_DIR`` named a bundle that will not load (missing,
    ``/dev/null``, not PEM). An ``OSError``, so every caller that maps a network failure
    still catches it; its own type, so a usage row and doctor can say what it is rather
    than "could not reach the usage endpoint (FileNotFoundError)"."""


class ProxyURLError(OSError):
    """``HTTPS_PROXY`` names no host and port that can be dialled (``http://proxy:abc``). An
    ``OSError``, so a caller that maps a network failure still catches it; its text names
    the variable and why, never the URL, which may carry credentials (review of #403, m2)."""


def proxy_endpoint(proxy: str) -> tuple[str, int, str, str]:
    """``(host, port, user, password)`` of ``proxy`` (an ``HTTPS_PROXY`` value), 443 for no
    port as urllib's tunnel dials it; :class:`ProxyURLError` for one that cannot be read."""
    from urllib.parse import unquote, urlsplit

    try:
        parts = urlsplit(proxy if "://" in proxy else f"http://{proxy}")
        host, port = parts.hostname, parts.port
    except ValueError as exc:  # a port that is no number, an IPv6 host left open
        raise ProxyURLError(f"HTTPS_PROXY is not a URL that can be dialled: {exc}") from None
    if not host:
        raise ProxyURLError("HTTPS_PROXY names no host")
    return host, port or 443, unquote(parts.username or ""), unquote(parts.password or "")


def _hardened(context: ssl.SSLContext) -> ssl.SSLContext:
    """``context`` with a certificate required, the hostname checked and TLS 1.2 the floor,
    whatever the interpreter or its OpenSSL would default to (review of #403, n9)."""
    import ssl

    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    return context


def _build() -> tuple[ssl.SSLContext, str]:
    context, source = _unhardened()
    return _hardened(context), source


def _unhardened() -> tuple[ssl.SSLContext, str]:
    import ssl

    cafile = os.environ.get("SSL_CERT_FILE") or None
    capath = os.environ.get("SSL_CERT_DIR") or None
    if cafile or capath:
        try:
            # The defaults, which OpenSSL takes these two variables INTO (SSL_CERT_FILE
            # replaces the default file), then the bundle named, loaded so one that will
            # not load raises: the stdlib's own context, trusting no more than it did.
            context = ssl.create_default_context()
            context.load_verify_locations(cafile=cafile, capath=capath)
            return context, SOURCE_ENV
        except OSError as exc:  # ssl.SSLError is one too
            named = " and ".join(
                f"{var}={value}"
                for var, value in (("SSL_CERT_FILE", cafile), ("SSL_CERT_DIR", capath))
                if value
            )
            raise CABundleError(f"{named}: {exc}") from exc
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


_per_thread = threading.local()
_generation = [0]
"""Bumped by :func:`forget_contexts`: every thread builds its context again at its next use."""


def _cached() -> tuple[ssl.SSLContext, str]:
    held = getattr(_per_thread, "built", None)
    if held is None or held[0] != _generation[0]:
        held = (_generation[0], _build())
        _per_thread.built = held
    built: tuple[ssl.SSLContext, str] = held[1]
    return built


def forget_contexts() -> None:
    """Build every thread's context again at its next use (a test that changes the env)."""
    _generation[0] += 1


def ssl_context() -> ssl.SSLContext:
    """This thread's verified client context: hostname checked, certificate required, TLS 1.2
    the floor. One per thread, never shared (see the module docstring, m1)."""
    return _cached()[0]


def trust_source() -> str:
    """Which rung of the order the context came from, for ``doctor``."""
    return _cached()[1]


def is_https(request: Any) -> bool:
    """Whether ``request`` (a URL or a ``urllib.request.Request``) is an ``https://`` one."""
    url = request if isinstance(request, str) else getattr(request, "full_url", "")
    return str(url).lower().startswith("https:")


def urlopen_without_redirects(request: Any, *, timeout: float) -> Any:
    """:func:`urlopen` that follows NO redirect: a 3xx comes back as the ``HTTPError`` it is.

    For a request that carries a credential (the usage shipper's bearer token): urllib follows
    a redirect by default, as a GET, to any origin, with the request's headers, and so would
    hand the token to wherever a gateway or a hijacked name pointed it. The caller treats a
    3xx as the failure it is."""
    import urllib.request

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(
            self, req: Any, fp: Any, code: Any, msg: Any, headers: Any, newurl: Any
        ) -> None:
            return None

    opener = urllib.request.build_opener(_NoRedirect(), https_handler())
    return opener.open(request, timeout=timeout)


def urlopen(request: Any, *, timeout: float) -> Any:
    """``urllib.request.urlopen`` with :func:`ssl_context`: the way this package opens a URL.

    An ``https://`` request needs the context, and fails as its build fails. A plain
    ``http://`` one gets it too when it builds, so a redirect to ``https://`` verifies as
    a direct request would (review of #403), and goes without it when it does not: an
    ``SSL_CERT_FILE`` that will not load must not fail a call that never speaks TLS —
    the explainability proxy's ``/health`` on loopback (review of #403, M2).

    Looked up on ``urllib.request`` at call time, so a test that replaces
    ``urllib.request.urlopen`` still intercepts it.
    """
    import urllib.request

    if is_https(request):
        return urllib.request.urlopen(request, timeout=timeout, context=ssl_context())
    try:
        context = ssl_context()
    except OSError:  # only a redirect would use it; the request itself is plain HTTP
        return urllib.request.urlopen(request, timeout=timeout)
    return urllib.request.urlopen(request, timeout=timeout, context=context)


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


def error_text(exc: BaseException) -> str:
    """What a TLS error says, for a row: its verify message when it has one, else its message.

    Not ``str(exc)``: an ``SSLError`` raised with one argument, as truststore raises a
    certificate the OS store refused, prints its args tuple, so the tls row read
    ``('"leaf" certificate is not trusted',)`` (TUI-401b (k) at e28697f1)."""
    reason = getattr(exc, "reason", None)
    if isinstance(reason, BaseException):  # a URLError around it
        return error_text(reason)
    message = getattr(exc, "verify_message", None)
    if isinstance(message, str) and message:
        return message
    if len(exc.args) == 1:
        return str(exc.args[0])
    return str(exc)


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

    proxy = https_proxy(host)
    if proxy is None:
        connection = http.client.HTTPSConnection(host, port, timeout=timeout, context=ssl_context())
    else:
        # No port: 443, the port urllib's own tunnel uses (its HTTPSConnection's default).
        where, through, user, password = proxy_endpoint(proxy)
        try:
            connection = http.client.HTTPSConnection(
                where, through, timeout=timeout, context=ssl_context()
            )
        except http.client.InvalidURL as exc:
            raise ProxyURLError(f"HTTPS_PROXY is not a URL that can be dialled: {exc}") from None
        headers = {}
        if user:
            secret = f"{user}:{password}"
            headers["Proxy-Authorization"] = "Basic " + base64.b64encode(secret.encode()).decode()
        connection.set_tunnel(host, port, headers=headers)
    try:
        connection.connect()
    finally:
        connection.close()
