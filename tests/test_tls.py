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


def _violations(path: Path) -> list[str]:
    """Every way ``path`` could open HTTPS without :func:`aisquare.core.tls.ssl_context`."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []

    def through_tls(node: ast.Call) -> bool:
        for keyword in node.keywords:
            value = keyword.value
            if (
                keyword.arg == "context"
                and isinstance(value, ast.Call)
                and isinstance(value.func, ast.Attribute)
                and value.func.attr == "ssl_context"
                and isinstance(value.func.value, ast.Name)
                and value.func.value.id == "tls"
            ):
                return True
        return False

    for node in ast.walk(tree):
        where = f"{path.name}:{getattr(node, 'lineno', '?')}"
        if isinstance(node, ast.ImportFrom) and node.module == "urllib.request":
            if any(alias.name == "urlopen" for alias in node.names):
                found.append(f"{where} imports urllib.request.urlopen")
        elif isinstance(node, ast.Attribute) and node.attr == "urlopen":
            if isinstance(node.value, ast.Attribute) and node.value.attr == "request":
                found.append(f"{where} calls urllib.request.urlopen")
        elif isinstance(node, ast.Attribute) and node.attr in {
            "create_default_context",
            "_create_unverified_context",
        }:
            found.append(f"{where} builds its own SSL context ({node.attr})")
        elif isinstance(node, ast.Call):
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name in {"HTTPSConnection", "HTTPSHandler"} and not through_tls(node):
                found.append(f"{where} {name} without context=tls.ssl_context()")
            if name == "build_opener" and not any(
                isinstance(arg, ast.Call)
                and (getattr(arg.func, "id", None) or getattr(arg.func, "attr", None))
                == "HTTPSHandler"
                for arg in node.args
            ):
                found.append(f"{where} build_opener without an HTTPSHandler")
    return found


def test_no_module_opens_https_without_the_shared_context() -> None:
    home = SRC / "core" / "tls.py"
    found = [v for path in sorted(SRC.rglob("*.py")) if path != home for v in _violations(path)]
    assert found == [], "\n".join(found)


def test_the_guard_catches_each_way_around_it(tmp_path: Path) -> None:
    """The negative beside the positive: a guard that finds nothing must be able to."""
    bad = tmp_path / "probe.py"
    sources = {
        "from urllib.request import urlopen\n": "imports urllib.request.urlopen",
        "import urllib.request\nurllib.request.urlopen('https://x')\n": "calls urllib",
        "import ssl\nssl.create_default_context()\n": "builds its own SSL context",
        "import http.client\nhttp.client.HTTPSConnection('x')\n": "HTTPSConnection without",
        "from urllib.request import build_opener\nbuild_opener()\n": "build_opener without",
    }
    for source, expected in sources.items():
        bad.write_text(source, encoding="utf-8")
        [violation] = _violations(bad)
        assert expected in violation, (source, violation)
    bad.write_text(
        "from aisquare.core import tls\nimport http.client\n"
        "http.client.HTTPSConnection('x', context=tls.ssl_context())\n",
        encoding="utf-8",
    )
    assert _violations(bad) == []


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
