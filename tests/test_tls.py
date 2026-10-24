"""Every HTTPS call verifies through ``core.tls``, and a certificate failure says so.

On a python.org Python on macOS the interpreter's own CA paths are empty, so
``urlopen`` with no context failed CERTIFICATE_VERIFY_FAILED on every request and
each usage row said "could not reach the usage endpoint (URLError)" — while curl,
asking the system trust store, worked. These tests run a local HTTPS server whose
certificate is signed by a private CA, which no default store contains: the
stdlib default context must fail it (the control), and the shared context, given
that CA as its bundle, must pass it. Nothing here leaves the machine.
"""

from __future__ import annotations

import ast
import contextlib
import json
import ssl
import sys
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import IPv4Address
from pathlib import Path
from typing import Any, NamedTuple

import pytest

from aisquare.core import claude_accounts as core
from aisquare.core import tls
from aisquare.services import claude_accounts as service
from tests.test_claude_accounts import LIVE_USAGE, NOW, _sign_in

SRC = Path(__file__).resolve().parent.parent / "src" / "aisquare"


class _Server(NamedTuple):
    url: str
    ca_file: Path


def _private_ca(directory: Path) -> tuple[Path, Path, Path]:
    """A CA, and a leaf for 127.0.0.1 it signed: (ca.pem, leaf.pem, leaf.key)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    start = datetime.now(tz=UTC) - timedelta(hours=1)
    end = start + timedelta(days=2)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "aisquare test CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(end)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")]))
        .issuer_name(ca_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(end)
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(IPv4Address("127.0.0.1"))]),
            critical=False,
        )
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    pem = serialization.Encoding.PEM
    ca_file, leaf_file, key_file = (
        directory / "ca.pem",
        directory / "leaf.pem",
        directory / "leaf.key",
    )
    ca_file.write_bytes(ca.public_bytes(pem))
    leaf_file.write_bytes(leaf.public_bytes(pem))
    key_file.write_bytes(
        leaf_key.private_bytes(pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return ca_file, leaf_file, key_file


class _Usage(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = json.dumps(LIVE_USAGE).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: Any) -> None:
        pass


@pytest.fixture
def https_server(tmp_path: Path) -> Iterator[_Server]:
    """The usage endpoint's shape, over TLS, on a certificate only ``ca_file`` vouches for."""
    ca_file, leaf_file, key_file = _private_ca(tmp_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Usage)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(leaf_file, key_file)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield _Server(f"https://127.0.0.1:{server.server_address[1]}/api/oauth/usage", ca_file)
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(autouse=True)
def fresh_context(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Each test builds its own context: the cache is per process, the environment per test."""
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    tls._cached.cache_clear()
    yield
    tls._cached.cache_clear()


@pytest.fixture
def no_default_cas(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A python.org Python on macOS: the interpreter's default CA paths hold nothing.

    ``load_default_certs`` is what ``create_default_context`` (and so a bare
    ``urlopen``) trusts; pointed at an empty directory, it trusts no one.
    """
    empty = tmp_path / "no-cas"
    empty.mkdir()

    def load_default_certs(self: ssl.SSLContext, purpose: Any = None) -> None:
        self.load_verify_locations(capath=str(empty))

    monkeypatch.setattr(ssl.SSLContext, "load_default_certs", load_default_certs)


def _certifi_only(monkeypatch: pytest.MonkeyPatch, bundle: Path) -> None:
    """No truststore, and certifi's bundle is ``bundle``: the fallback rung, pinned."""
    import certifi

    monkeypatch.setitem(sys.modules, "truststore", None)
    monkeypatch.setattr(certifi, "where", lambda: str(bundle))


# --------------------------------------------------------------------------- the context


def test_http_get_verifies_through_the_shared_context_where_the_default_paths_have_no_ca(
    https_server: _Server, no_default_cas: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The control, and the bug: a bare urlopen trusts only the (empty) default paths.
    with pytest.raises(urllib.error.URLError) as bare:
        urllib.request.urlopen(https_server.url, timeout=5)
    assert isinstance(bare.value.reason, ssl.SSLCertVerificationError)

    _certifi_only(monkeypatch, https_server.ca_file)
    status, body = service._http_get(https_server.url, {}, 5)

    assert status == 200 and json.loads(body) == LIVE_USAGE
    assert tls.trust_source() == tls.SOURCE_CERTIFI


def test_usage_reads_percentages_over_the_shared_context(
    fake_home: Path,
    https_server: _Server,
    no_default_cas: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _certifi_only(monkeypatch, https_server.ca_file)
    monkeypatch.setattr(service, "USAGE_URL", https_server.url)
    account = core.create_account()
    _sign_in(account, "two@example.com")

    usage = service.usage(account, now=NOW)

    assert usage.available, usage.reason
    assert usage.session_percent == 3.0 and usage.week_percent == 7.0


def test_the_operators_bundle_wins_over_truststore_and_certifi(
    monkeypatch: pytest.MonkeyPatch, https_server: _Server
) -> None:
    monkeypatch.setenv("SSL_CERT_FILE", str(https_server.ca_file))
    assert tls.trust_source() == tls.SOURCE_ENV
    assert service._http_get(https_server.url, {}, 5)[0] == 200


def test_truststore_is_preferred_and_certifi_is_the_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import truststore

    context = tls.ssl_context()
    assert isinstance(context, truststore.SSLContext)
    assert tls.trust_source() == tls.SOURCE_TRUSTSTORE
    assert context.verify_mode is ssl.CERT_REQUIRED and context.check_hostname

    tls._cached.cache_clear()
    monkeypatch.setitem(sys.modules, "truststore", None)
    fallback = tls.ssl_context()
    assert tls.trust_source() == tls.SOURCE_CERTIFI
    assert fallback.verify_mode is ssl.CERT_REQUIRED and fallback.check_hostname
    assert tls.ssl_context() is fallback  # built once per process


# --------------------------------------------------------------------------- the reason


def test_a_certificate_that_does_not_verify_is_named_as_tls_not_urlerror(
    fake_home: Path,
    https_server: _Server,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """End to end: the shared context does not know the server's CA."""
    (tmp_path / "other").mkdir()
    other_ca, _, _ = _private_ca(tmp_path / "other")
    _certifi_only(monkeypatch, other_ca)
    monkeypatch.setattr(service, "USAGE_URL", https_server.url)
    account = core.create_account()
    _sign_in(account, "two@example.com")

    usage = service.usage(account, now=NOW)

    assert not usage.available and usage.reason is not None
    assert usage.reason.startswith("TLS certificate could not be verified — ")
    assert "URLError" not in usage.reason and "sk-ant" not in usage.reason
    assert not usage.needs_login  # a log-in would not fix a trust store


@pytest.mark.parametrize(
    ("error", "tls_reason"),
    [
        (
            urllib.error.URLError(ssl.SSLCertVerificationError(1, "certificate verify failed")),
            True,
        ),
        (ssl.SSLCertVerificationError(1, "certificate verify failed"), True),
        (urllib.error.URLError(ConnectionRefusedError("refused")), False),
        (urllib.error.URLError(ssl.SSLError(1, "wrong version number")), False),
    ],
    ids=["wrapped", "bare", "refused", "other-ssl-error"],
)
def test_only_a_certificate_failure_gets_the_tls_reason(
    fake_home: Path, error: OSError, tls_reason: bool
) -> None:
    def fetch(*_args: Any) -> tuple[int, bytes]:
        raise error

    account = core.create_account()
    _sign_in(account, "two@example.com")
    reason = service.usage(account, now=NOW, fetch=fetch).reason or ""
    assert reason.startswith("TLS certificate could not be verified") is tls_reason
    assert ("could not reach the usage endpoint" in reason) is not tls_reason


# --------------------------------------------------------------------------- the guard


#: Names that open a URL or build or weaken a TLS context. Matched as a NAME, however it
#: was reached — ``urllib.request.urlopen``, ``ur.urlopen`` after ``import urllib.request
#: as ur``, ``request.urlopen`` after ``from urllib import request``, or a bare
#: ``urlopen`` imported under any alias — so an alias is no way around the guard
#: (review of #403, M1). ``tls.urlopen`` is the one attribute spelling allowed.
_OPENS = {"urlopen", "urlretrieve"}
_BUILDS_CONTEXT = {"SSLContext", "create_default_context", "_create_unverified_context"}
_UNVERIFIED = {"CERT_NONE"}
#: Modules a forbidden name may not be imported from (``from aisquare.core.tls import
#: urlopen`` is the shared one, and allowed).
_STDLIB_TLS = {"urllib", "urllib.request", "ssl", "_ssl"}


def _is_tls_call(node: ast.AST, attr: str) -> bool:
    """``tls.<attr>(...)``."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == attr
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "tls"
    )


def _violations(path: Path) -> list[str]:
    """Every way ``path`` could open HTTPS without :func:`aisquare.core.tls.ssl_context`,
    or turn verification off."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []

    def through_tls(node: ast.Call) -> bool:
        return any(
            keyword.arg == "context" and _is_tls_call(keyword.value, "ssl_context")
            for keyword in node.keywords
        )

    def false_hostname_check(target: ast.AST, value: ast.AST) -> bool:
        return (
            isinstance(target, ast.Attribute)
            and target.attr == "check_hostname"
            and isinstance(value, ast.Constant)
            and value.value is False
        )

    for node in ast.walk(tree):
        where = f"{path.name}:{getattr(node, 'lineno', '?')}"
        if isinstance(node, ast.ImportFrom) and node.module in _STDLIB_TLS:
            for alias in node.names:
                if alias.name == "*" or alias.name in _OPENS | _BUILDS_CONTEXT | _UNVERIFIED:
                    found.append(f"{where} imports {node.module}.{alias.name}")
        elif isinstance(node, ast.Attribute) and node.attr in _OPENS:
            if not (isinstance(node.value, ast.Name) and node.value.id == "tls"):
                found.append(f"{where} opens a URL with {node.attr}, not tls.urlopen")
        elif isinstance(node, ast.Attribute) and node.attr in _BUILDS_CONTEXT:
            found.append(f"{where} builds its own SSL context ({node.attr})")
        elif isinstance(node, (ast.Attribute, ast.Name)) and (
            getattr(node, "attr", None) in _UNVERIFIED or getattr(node, "id", None) in _UNVERIFIED
        ):
            found.append(f"{where} turns certificate verification off (CERT_NONE)")
        elif isinstance(node, ast.Assign) and any(
            false_hostname_check(target, node.value) for target in node.targets
        ):
            found.append(f"{where} turns hostname checking off (check_hostname = False)")
        elif isinstance(node, ast.Call):
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if any(
                keyword.arg == "check_hostname"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is False
                for keyword in node.keywords
            ):
                found.append(f"{where} turns hostname checking off (check_hostname=False)")
            if name in {"HTTPSConnection", "HTTPSHandler"} and not through_tls(node):
                found.append(f"{where} {name} without context=tls.ssl_context()")
            if name == "build_opener" and not any(
                _is_tls_call(arg, "https_handler")
                or (
                    isinstance(arg, ast.Call)
                    and (getattr(arg.func, "attr", None) or getattr(arg.func, "id", None))
                    == "HTTPSHandler"
                    and through_tls(arg)
                )
                for arg in node.args
            ):
                found.append(f"{where} build_opener without tls.https_handler()")
    return found


def test_no_module_opens_https_without_the_shared_context() -> None:
    home = SRC / "core" / "tls.py"
    found = [v for path in sorted(SRC.rglob("*.py")) if path != home for v in _violations(path)]
    assert found == [], "\n".join(found)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("from urllib.request import urlopen\n", "imports urllib.request.urlopen"),
        ("from urllib.request import urlopen as fetch\n", "imports urllib.request.urlopen"),
        ("from urllib.request import *\n", "imports urllib.request.*"),
        ("import urllib.request\nurllib.request.urlopen('https://x')\n", "not tls.urlopen"),
        ("import urllib.request as ur\nur.urlopen('https://x')\n", "not tls.urlopen"),
        ("from urllib import request\nrequest.urlopen('https://x')\n", "not tls.urlopen"),
        ("import urllib.request\nurllib.request.urlretrieve('https://x')\n", "urlretrieve"),
        ("from urllib.request import urlretrieve\n", "imports urllib.request.urlretrieve"),
        ("import ssl\nssl.create_default_context()\n", "builds its own SSL context"),
        ("import ssl\nssl._create_unverified_context()\n", "builds its own SSL context"),
        ("import ssl\nssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)\n", "builds its own SSL context"),
        ("from ssl import SSLContext as S\n", "imports ssl.SSLContext"),
        ("import ssl as s\nx = s.CERT_NONE\n", "CERT_NONE"),
        ("from ssl import CERT_NONE\n", "imports ssl.CERT_NONE"),
        ("context = make()\ncontext.check_hostname = False\n", "check_hostname = False"),
        ("handler(check_hostname=False)\n", "check_hostname=False"),
        ("import http.client\nhttp.client.HTTPSConnection('x')\n", "HTTPSConnection without"),
        ("from urllib.request import build_opener\nbuild_opener()\n", "build_opener without"),
    ],
)
def test_the_guard_catches_each_way_around_it(tmp_path: Path, source: str, expected: str) -> None:
    """The negative beside the positive: a guard that finds nothing must be able to."""
    bad = tmp_path / "probe.py"
    bad.write_text(source, encoding="utf-8")
    [violation] = _violations(bad)
    assert expected in violation, (source, violation)


def test_the_guard_passes_the_shared_spellings(tmp_path: Path) -> None:
    good = tmp_path / "probe.py"
    good.write_text(
        "import http.client\n"
        "from urllib.request import Request, build_opener\n"
        "from aisquare.core import tls\n"
        "from aisquare.core.tls import urlopen\n"
        "http.client.HTTPSConnection('x', context=tls.ssl_context())\n"
        "build_opener(tls.https_handler())\n"
        "tls.urlopen(Request('https://x'), timeout=1)\n"
        "urlopen('https://x', timeout=1)\n",
        encoding="utf-8",
    )
    assert _violations(good) == []


# --------------------------------------------------------------------------- doctor --live


@pytest.mark.parametrize(
    ("raised", "status", "says"),
    [
        (None, "ok", "api.anthropic.com verified against"),
        (ssl.SSLCertVerificationError(1, "certificate verify failed"), "fail", "did not verify"),
        (ConnectionRefusedError("refused"), "warn", "could not reach api.anthropic.com"),
    ],
    ids=["verified", "cert-failure", "offline"],
)
def test_doctors_tls_row_names_the_trust_source_and_the_failure(
    monkeypatch: pytest.MonkeyPatch, raised: OSError | None, status: str, says: str
) -> None:
    from aisquare.services import diagnostics

    asked: list[str] = []

    def handshake(host: str) -> None:
        asked.append(host)
        if raised is not None:
            raise raised

    monkeypatch.setattr(tls, "handshake", handshake)
    row = diagnostics._check_tls()
    assert asked == ["api.anthropic.com"]
    assert (row.name, row.status.value) == ("tls", status) and says in row.detail
    if status == "ok":
        assert tls.trust_source() in row.detail


def test_doctors_tls_row_fails_on_a_bundle_that_will_not_load(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from aisquare.services import diagnostics

    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "missing.pem"))
    monkeypatch.setattr(tls, "handshake", lambda host: pytest.fail("no bundle, no handshake"))
    row = diagnostics._check_tls()
    assert row.status.value == "fail" and "could not be loaded" in row.detail


def test_doctors_tls_row_is_live_only() -> None:
    from aisquare.services import diagnostics

    source = Path(diagnostics.__file__).read_text(encoding="utf-8")
    assert "*_live_only(live, _check_tls)," in source


# --------------------------------------------------------------------------- http stays http


class _Health(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = json.dumps({"status": "ok"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: Any) -> None:
        pass


@pytest.fixture
def http_server() -> Iterator[str]:
    """A plain ``http://`` server on loopback, as the explainability proxy is."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Health)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_a_bundle_that_will_not_load_fails_https_and_leaves_plain_http_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, http_server: str, https_server: _Server
) -> None:
    """Review of #403, M2: the context went to every call, so an ``SSL_CERT_FILE`` that will
    not load failed a ``http://`` call that never speaks TLS — the explainability proxy's
    ``/health`` said "[Errno 2] No such file" where main reached it."""
    from aisquare.services import ci_client, explainability

    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "missing.pem"))

    with tls.urlopen(f"{http_server}/health", timeout=5) as response:
        assert response.status == 200
    exchanged = ci_client.exchange(f"{http_server}/health", method="GET", deadline_ms=5000)
    assert exchanged.status == 200 and exchanged.reason is None
    probe = explainability.probe_proxy(http_server)
    assert "No such file" not in probe.reason  # it reached the server and read its answer
    # The control: https is still refused, and says why, in each spelling.
    with pytest.raises(OSError):
        tls.urlopen(https_server.url, timeout=5)
    refused = ci_client.exchange(https_server.url, method="GET", deadline_ms=5000)
    assert refused.status is None and refused.reason is not None


# --------------------------------------------------------------------------- elapsed_ms


@pytest.mark.parametrize("scheme", ["https", "http"])
def test_the_ci_exchange_builds_the_context_before_its_clock_and_only_for_https(
    monkeypatch: pytest.MonkeyPatch, scheme: str
) -> None:
    """Review of #403: the trust store's first build (~15 ms against the stdlib's ~3 ms)
    landed inside the exchange's ``elapsed_ms``, the latency the CI experiment measures."""
    from aisquare.services import ci_client

    events: list[tuple[str, float]] = []
    monkeypatch.setattr(tls, "ssl_context", lambda: events.append(("context", 0.0)))

    def exchange(url: str, *, started: float, **_: Any) -> Any:
        events.append(("started", started))
        return ci_client.Exchange(status=200, body="", elapsed_ms=0, reason=None, detail="")

    monkeypatch.setattr(ci_client, "_blocking_exchange", exchange)
    ci_client.exchange(f"{scheme}://ci.example/run", method="GET", deadline_ms=5000)

    assert [name for name, _ in events] == (
        ["context", "started"] if scheme == "https" else ["started"]
    )


# --------------------------------------------------------------------------- HTTPS_PROXY


@pytest.fixture
def connect_proxy() -> Iterator[tuple[str, list[str]]]:
    """A one-purpose ``CONNECT`` proxy: tunnels each request, records each request line."""
    import socket
    import socketserver

    seen: list[str] = []

    class Tunnel(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = self.request.recv(4096)
                if not chunk:
                    return
                head += chunk
            line = head.split(b"\r\n", 1)[0].decode()
            seen.append(line)
            host, port = line.split()[1].rsplit(":", 1)
            upstream = socket.create_connection((host, int(port)), timeout=5)
            self.request.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")

            def pump(src: socket.socket, dst: socket.socket) -> None:
                try:
                    while data := src.recv(65536):
                        dst.sendall(data)
                except OSError:
                    pass
                finally:
                    for end in (src, dst):
                        with contextlib.suppress(OSError):
                            end.shutdown(socket.SHUT_RDWR)

            back = threading.Thread(target=pump, args=(upstream, self.request), daemon=True)
            back.start()
            pump(self.request, upstream)
            back.join(5)
            upstream.close()

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Tunnel)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", seen
    finally:
        server.shutdown()
        server.server_close()


def test_the_handshake_goes_through_https_proxy_as_requests_do(
    monkeypatch: pytest.MonkeyPatch,
    https_server: _Server,
    connect_proxy: tuple[str, list[str]],
) -> None:
    """Review of #403: doctor's handshake went direct, so a machine that may only leave
    through its proxy failed the row while its usage reads worked."""
    proxy, seen = connect_proxy
    _certifi_only(monkeypatch, https_server.ca_file)
    port = int(https_server.url.split(":")[2].split("/")[0])
    for name in ("HTTPS_PROXY", "https_proxy"):
        monkeypatch.setenv(name, proxy)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)

    assert tls.https_proxy("127.0.0.1") == proxy
    tls.handshake("127.0.0.1", port=port, timeout=5)
    assert seen == [f"CONNECT 127.0.0.1:{port} HTTP/1.1"]

    monkeypatch.setenv("NO_PROXY", "127.0.0.1")  # the control: bypassed, it goes direct
    assert tls.https_proxy("127.0.0.1") is None
    tls.handshake("127.0.0.1", port=port, timeout=5)
    assert len(seen) == 1


def test_doctors_tls_row_names_the_proxy_without_its_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aisquare.services import diagnostics

    monkeypatch.setattr(tls, "https_proxy", lambda host: "http://me:hunter2@proxy.corp:3128")
    monkeypatch.setattr(tls, "handshake", lambda host: None)
    row = diagnostics._check_tls()
    assert row.status.value == "ok" and "through the proxy proxy.corp:3128" in row.detail
    assert "hunter2" not in row.detail and "me:" not in row.detail
