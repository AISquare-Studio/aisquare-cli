"""Web Push (SPEC §5): the crypto, the allowlist, the file, the routes, and ngrok.

No test here reaches the network. The one-shot pushes take an injected
transport, and an autouse guard stands in for the real one and for ngrok's
agent API, so a test that forgot to inject fails instead of POSTing to a push
service.

Each push is DECRYPTED here, by a receiver written against RFC 8291 §3.4 with
``cryptography``'s own HKDF rather than the module's hand-rolled HMACs, so a
test reads exactly what a phone would.
"""

from __future__ import annotations

import base64
import json
import os
import stat
import struct
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from starlette.testclient import TestClient

from aisquare.core.paths import remote_audit_path, remote_push_path
from aisquare.services import ngrok_tunnel, remote_push
from aisquare.services.ngrok_tunnel import (
    TOO_OLD_HINT,
    NgrokTunnel,
    discover_ngrok_public_url,
    ngrok_command,
    ngrok_static_host,
    parse_log_line,
)
from aisquare.services.remote_push import (
    PUSH_INSTALL_HINT,
    TEST_TITLE,
    PushSubscriptionRecord,
    encrypt_push_payload,
    load_or_create_vapid_keys,
    load_push_state,
    push_host_allowed,
    push_record_outcome,
    push_send_one,
    push_subscribe_device,
    push_subscription_from_body,
    push_unsubscribe_device,
    start_push_sender,
    vapid_authorization,
)
from aisquare.services.remote_server import RemoteKit, Runtime, Sources, build_app
from tests.remote_kit_helpers import base, make_client, make_runtime, unlock

REAL_TRANSPORT = remote_push.push_https_transport
"""Kept before the guard below replaces it, for the one test of the real transport."""

T0 = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)
FCM = "https://fcm.googleapis.com/fcm/send/"


# --- stand-ins and guards ---------------------------------------------------------------------


@pytest.fixture(autouse=True)
def no_push_service(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """The real transport and ngrok's agent API, replaced by refusals the test fails on:
    a test that forgot to hand in its own transport is red, never a request on the wire."""
    reached: list[str] = []

    def refuse_push(endpoint: str, headers: dict[str, str], body: bytes) -> int:
        reached.append(endpoint)
        raise OSError("a test reached the real push transport")

    def refuse_agent_api(url: str, timeout: float) -> bytes:
        reached.append(url)
        raise OSError("a test reached ngrok's real agent API")

    monkeypatch.setattr(remote_push, "push_https_transport", refuse_push)
    monkeypatch.setattr(ngrok_tunnel, "_ngrok_agent_api_get", refuse_agent_api)
    yield reached
    assert reached == [], f"a test reached the network: {reached}"


@pytest.fixture
def roster(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    """The devices the runtime still has (signed in or signed out), as the test says.

    ``Runtime.device_ids`` is lane b-security's; this is the seam push reads it through,
    so the rules here are tested against a roster the test controls. The tests marked
    ``needs lane b-security`` read the real one.
    """
    live: set[str] = set()
    monkeypatch.setattr(remote_push, "push_device_ids", lambda kit: frozenset(live))
    return live


# --- a browser on the other end ---------------------------------------------------------------


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _point(private: ec.EllipticCurvePrivateKey) -> bytes:
    return private.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)


def read_push(body: bytes, ua_private: ec.EllipticCurvePrivateKey, auth_secret: bytes) -> bytes:
    """RFC 8291 §3.4 as a receiver does it: the keyid is the sender's key, and HKDF (not the
    module's HMACs) derives what decrypts the one record."""
    salt = body[:16]
    record_size, keyid_length = struct.unpack("!IB", body[16:21])
    assert (record_size, keyid_length) == (4096, 65)
    as_public = body[21:86]
    sender = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public)
    shared = ua_private.exchange(ec.ECDH(), sender)
    key_info = b"WebPush: info\x00" + _point(ua_private) + as_public
    ikm = HKDF(algorithm=hashes.SHA256(), length=32, salt=auth_secret, info=key_info).derive(shared)
    cek = HKDF(hashes.SHA256(), 16, salt=salt, info=b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(hashes.SHA256(), 12, salt=salt, info=b"Content-Encoding: nonce\x00").derive(ikm)
    padded = AESGCM(cek).decrypt(nonce, body[86:], None)
    assert padded.rstrip(b"\x00").endswith(b"\x02"), "one record, ending in the last-record mark"
    return padded.rstrip(b"\x00")[:-1]


@dataclass
class Browser:
    """A subscribed browser: its push service endpoint, its keys, and what pushes to it say."""

    endpoint: str
    private: ec.EllipticCurvePrivateKey = field(
        default_factory=lambda: ec.generate_private_key(ec.SECP256R1())
    )
    auth: bytes = field(default_factory=lambda: os.urandom(16))

    def subscription(self) -> dict[str, Any]:
        """What the page posts: ``PushSubscription.toJSON()``."""
        keys = {"p256dh": _b64(_point(self.private)), "auth": _b64(self.auth)}
        return {"endpoint": self.endpoint, "expirationTime": None, "keys": keys}

    def record(self) -> PushSubscriptionRecord:
        return push_subscription_from_body(self.subscription(), now=T0)

    def read(self, body: bytes) -> dict[str, Any]:
        payload = json.loads(read_push(body, self.private, self.auth))
        assert isinstance(payload, dict)
        return payload


class Transport:
    """A push service: answers each of ``statuses`` in turn (the last one from then on) and
    keeps every push it was handed."""

    def __init__(self, *statuses: int) -> None:
        self.statuses = list(statuses) or [201]
        self.sent: list[tuple[str, dict[str, str], bytes]] = []

    def __call__(self, endpoint: str, headers: dict[str, str], body: bytes) -> int:
        self.sent.append((endpoint, headers, body))
        return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]

    def wait_for(self, count: int, seconds: float = 10.0) -> None:
        deadline = time.monotonic() + seconds
        while len(self.sent) < count and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(self.sent) >= count, f"{len(self.sent)} pushes arrived, not {count}"


# --- the crypto (SPEC §5.5, §5.10 items 1-3) --------------------------------------------------


def test_a_push_decrypts_with_the_browsers_key_and_the_header_is_the_rfcs() -> None:
    browser = Browser(f"{FCM}x")
    body = encrypt_push_payload(b'{"v":1}', _point(browser.private), browser.auth)
    salt, (record_size, keyid_length) = body[:16], struct.unpack("!IB", body[16:21])
    assert len(salt) == 16 and record_size == 4096 and keyid_length == 65
    keyid = body[21:86]
    assert keyid[0] == 4, "the keyid is the sender's uncompressed P-256 key"
    ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), keyid)  # on the curve
    assert read_push(body, browser.private, browser.auth) == b'{"v":1}'
    again = encrypt_push_payload(b'{"v":1}', _point(browser.private), browser.auth)
    assert again[:16] != salt and again[21:86] != keyid, "a fresh salt and key every push"


def test_the_rfc_8291_example_is_reproduced_byte_for_byte() -> None:
    """RFC 8291 §5, transcribed: the keys, the secret and the salt it fixes give its message.
    The two public keys are checked against their private halves first, so a mistyped
    vector fails there and not as a crypto bug."""
    as_private = ec.derive_private_key(
        int.from_bytes(_unb64("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), "big"),
        ec.SECP256R1(),
    )
    ua_private = ec.derive_private_key(
        int.from_bytes(_unb64("q1dXpw3UpT5VOmu_cf_v6ih07Aems3njxI-JWgLcM94"), "big"),
        ec.SECP256R1(),
    )
    assert _b64(_point(as_private)) == (
        "BP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A8"
    )
    assert _b64(_point(ua_private)) == (
        "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"
    )
    plaintext = _unb64("V2hlbiBJIGdyb3cgdXAsIEkgd2FudCB0byBiZSBhIHdhdGVybWVsb24")
    assert plaintext == b"When I grow up, I want to be a watermelon"
    body = encrypt_push_payload(
        plaintext,
        _point(ua_private),
        _unb64("BTBZMqHH6r4Tts7J_aSIgg"),
        salt=_unb64("DGv6ra1nlYgDCS1FRnbzlw"),
        as_private=as_private,
    )
    assert _b64(body) == (
        "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLoc"
        "InmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLV"
        "WGNWQexSgSxsj_Qulcy4a-fN"
    )


def test_a_payload_that_does_not_fit_one_record_is_refused_not_split() -> None:
    browser = Browser(f"{FCM}x")
    with pytest.raises(ValueError):
        encrypt_push_payload(b"x" * 4080, _point(browser.private), browser.auth)
    with pytest.raises(ValueError):
        encrypt_push_payload(b"x", b"\x04" + b"\x01" * 64, browser.auth)  # not on the curve


def test_the_vapid_header_verifies_with_the_public_key_and_says_who_for_how_long(
    isolated_home: Path,
) -> None:
    keys = load_or_create_vapid_keys()
    header = vapid_authorization(f"{FCM}abc?x=1", keys, now=T0)
    assert header.startswith("vapid t=") and header.endswith(f", k={keys.public_key}")
    jwt = header.removeprefix("vapid t=").split(", k=")[0]
    head, claims, signature = jwt.split(".")
    assert json.loads(_unb64(head)) == {"typ": "JWT", "alg": "ES256"}
    body = json.loads(_unb64(claims))
    assert body["aud"] == "https://fcm.googleapis.com", "the origin, without path or query"
    assert body["sub"] == "https://github.com/AISquare-Studio/aisquare-cli"
    assert body["exp"] == int((T0 + timedelta(hours=12)).timestamp())
    assert body["exp"] - T0.timestamp() <= 24 * 3600
    raw = _unb64(signature)
    assert len(raw) == 64, "raw r || s, not DER"
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), _unb64(keys.public_key))
    public.verify(der, f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))


def test_one_vapid_signature_serves_a_push_service_for_an_hour(isolated_home: Path) -> None:
    keys = load_or_create_vapid_keys()
    first = vapid_authorization(f"{FCM}a", keys, now=T0)
    assert vapid_authorization(f"{FCM}b", keys, now=T0 + timedelta(minutes=59)) == first
    other = vapid_authorization("https://web.push.apple.com/x", keys, now=T0)
    assert other != first, "one per push service: the audience is signed"
    assert vapid_authorization(f"{FCM}a", keys, now=T0 + timedelta(minutes=61)) != first


# --- which push services (SPEC §5.4, §5.10 item 4) --------------------------------------------


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://fcm.googleapis.com/fcm/send/x",  # not https
        "https://fcm.googleapis.com.evil.com/fcm/send/x",  # a suffix spoof
        "https://user@fcm.googleapis.com/fcm/send/x",  # userinfo
        "https://fcm.googleapis.com:8443/fcm/send/x",  # another port
        "https://127.0.0.1/x",  # an IP literal
        "https://[::1]/x",
        "https://evilpush.apple.com/x",  # push.apple.com needs the dot
        "https://notify.windows.com/x",  # the bare suffix is no shard
        "https://fcm.googleapis.com./x",  # another name than fcm.googleapis.com
        "https://fcm.googleapis.com/fcm send/x",  # whitespace
        "https://fcm.googleapis.com/x\r\nHost: evil",
        "https://fcm.googleapis.cöm/x",
        "https:///x",
        "not a url",
    ],
)
def test_a_push_endpoint_off_the_allowlist_is_refused(endpoint: str) -> None:
    assert push_host_allowed(endpoint) is False


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://fcm.googleapis.com/fcm/send/dQw4w9WgXcQ:APA91b",
        "https://updates.push.services.mozilla.com/wpush/v2/gAAAAAB",
        "https://web.push.apple.com/QGuQyavXutnMH7",
        "https://api.push.apple.com/3/device/abc",
        "https://wns2-par02p.notify.windows.com/w/?token=BQYAAAB",
        "https://FCM.googleapis.com:443/fcm/send/x",
    ],
)
def test_the_four_real_push_services_are_accepted(endpoint: str) -> None:
    assert push_host_allowed(endpoint) is True


def test_the_real_transport_refuses_an_endpoint_off_the_allowlist_before_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import http.client

    def no_connection(*args: object, **kwargs: object) -> None:
        raise AssertionError("a connection was opened")

    monkeypatch.setattr(http.client, "HTTPSConnection", no_connection)
    with pytest.raises(ValueError):
        REAL_TRANSPORT("https://evil.example/x", {}, b"")


def test_the_real_transport_posts_once_on_443_verified_and_reads_little(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No redirect followed, no proxy, the certificate checked, and only 4 KiB of answer."""
    import http.client
    import ssl

    opened: list[dict[str, Any]] = []

    class Answer:
        status = 201

        def read(self, amount: int) -> bytes:
            opened[-1]["read"] = amount
            return b""

    class Connection:
        def __init__(self, host: str, port: int, **kwargs: Any) -> None:
            opened.append({"host": host, "port": port, **kwargs, "closed": False})

        def request(self, method: str, target: str, body: bytes, headers: dict[str, str]) -> None:
            opened[-1].update(method=method, target=target, body=body)

        def getresponse(self) -> Answer:
            return Answer()

        def close(self) -> None:
            opened[-1]["closed"] = True

    monkeypatch.setattr(http.client, "HTTPSConnection", Connection)
    status = REAL_TRANSPORT(f"{FCM}abc?x=1", {"TTL": "3600"}, b"sealed")
    assert status == 201
    (connection,) = opened
    assert (connection["host"], connection["port"]) == ("fcm.googleapis.com", 443)
    assert (connection["method"], connection["target"]) == ("POST", "/fcm/send/abc?x=1")
    assert connection["timeout"] == 10.0 and connection["read"] == 4096 and connection["closed"]
    context = connection["context"]
    assert isinstance(context, ssl.SSLContext) and context.verify_mode == ssl.CERT_REQUIRED


# --- the file (SPEC §5.2, §5.3, §5.10 item 5) -------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_the_push_file_is_0600(isolated_home: Path) -> None:
    load_or_create_vapid_keys()
    assert stat.S_IMODE(remote_push_path().stat().st_mode) == 0o600


def test_the_push_file_is_restricted_before_it_holds_the_key(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every platform, Windows included, where the 0600 above is skipped and the restriction
    is the whole protection: the temp the key is written into is restricted while empty."""
    from aisquare.core import paths

    restricted: list[tuple[str, int]] = []
    real = paths.restrict_to_owner

    def spy(path: Path) -> bool:
        restricted.append((path.name, path.stat().st_size))
        return real(path)

    monkeypatch.setattr(paths, "restrict_to_owner", spy)
    keys = load_or_create_vapid_keys()
    ((temp, size),) = restricted
    assert temp.startswith(f".{remote_push_path().name}.") and size == 0
    assert keys.private_key in remote_push_path().read_text(encoding="utf-8")


def test_the_vapid_keys_are_made_once_and_kept(isolated_home: Path) -> None:
    keys = load_or_create_vapid_keys()
    assert load_or_create_vapid_keys() == keys
    assert len(_unb64(keys.public_key)) == 65 and len(_unb64(keys.private_key)) == 32
    private = ec.derive_private_key(int.from_bytes(_unb64(keys.private_key), "big"), ec.SECP256R1())
    assert _b64(_point(private)) == keys.public_key
    raw = json.loads(remote_push_path().read_text(encoding="utf-8"))
    assert raw["version"] == 1 and raw["vapid"]["public_key"] == keys.public_key


def test_a_malformed_push_file_starts_over_instead_of_failing(isolated_home: Path) -> None:
    remote_push_path().parent.mkdir(parents=True, exist_ok=True)
    remote_push_path().write_text("{not json", encoding="utf-8")
    assert load_push_state().subscriptions == {}
    assert len(_unb64(load_or_create_vapid_keys().public_key)) == 65


def test_subscriptions_of_devices_the_runtime_no_longer_has_go_at_the_next_write(
    isolated_home: Path,
) -> None:
    """A revoked, expired or regenerated-away device is not in the roster; a device signed
    out for being idle is, and keeps its subscription (SPEC §5.3)."""
    signed_in, idle, revoked = (Browser(f"{FCM}{n}") for n in ("in", "idle", "gone"))
    everyone = {"dev_in", "dev_idle", "dev_gone"}
    for device, browser in zip(sorted(everyone), (revoked, idle, signed_in), strict=True):
        push_subscribe_device(device, browser.record(), everyone)
    newcomer = Browser(f"{FCM}new")
    push_subscribe_device("dev_new", newcomer.record(), {"dev_in", "dev_idle", "dev_new"})
    assert set(load_push_state().subscriptions) == {"dev_in", "dev_idle", "dev_new"}
    push_unsubscribe_device("dev_new", {"dev_in"})
    assert set(load_push_state().subscriptions) == {"dev_in"}, "an unsubscribe is a write too"


def test_the_same_endpoint_from_another_device_moves_there(isolated_home: Path) -> None:
    """The phone unlocked again into a new device (a new ngrok origin, its iOS app): one
    subscription, so one push per item, not two."""
    phone = Browser(f"{FCM}phone")
    push_subscribe_device("dev_old", phone.record(), {"dev_old", "dev_new"})
    push_subscribe_device("dev_new", phone.record(), {"dev_old", "dev_new"})
    assert set(load_push_state().subscriptions) == {"dev_new"}


def test_a_resubscription_replaces_the_devices_old_one(isolated_home: Path) -> None:
    first, second = Browser(f"{FCM}1"), Browser(f"{FCM}2")
    push_subscribe_device("dev_a", first.record(), {"dev_a"})
    push_subscribe_device("dev_a", second.record(), {"dev_a"})
    assert load_push_state().subscriptions["dev_a"].endpoint == second.endpoint


# --- what the push service says (SPEC §5.7, §5.10 item 6) -------------------------------------


@pytest.mark.parametrize(
    ("statuses", "kept", "failures"),
    [
        ((201,), True, 0),
        ((202,), True, 0),
        ((410,), False, None),
        ((404,), False, None),
        ((429,), True, 1),
        ((503,), True, 1),
        ((413,), True, 0),
        ((403, 403), True, 2),
        ((403, 403, 403), False, None),
        ((401, 400, 403), False, None),
        ((403, 403, 201), True, 0),
    ],
)
def test_each_answer_keeps_counts_or_drops_the_subscription(
    isolated_home: Path, statuses: tuple[int, ...], kept: bool, failures: int | None
) -> None:
    browser = Browser(f"{FCM}x")
    push_subscribe_device("dev_a", browser.record(), {"dev_a"})
    keys = load_or_create_vapid_keys()
    transport = Transport(*statuses)
    for _ in statuses:
        record = load_push_state().subscriptions["dev_a"]
        push_send_one("dev_a", record, {"title": "t"}, keys=keys, transport=transport, now=T0)
    held = load_push_state().subscriptions.get("dev_a")
    assert (held is not None) is kept
    if held is not None:
        assert held.failures == failures


def test_no_answer_at_all_counts_as_a_failure_and_keeps_it(isolated_home: Path) -> None:
    browser = Browser(f"{FCM}x")
    push_subscribe_device("dev_a", browser.record(), {"dev_a"})

    def unreachable(endpoint: str, headers: dict[str, str], body: bytes) -> int:
        raise TimeoutError("the push service did not answer in 10 s")

    record = load_push_state().subscriptions["dev_a"]
    keys = load_or_create_vapid_keys()
    status = push_send_one("dev_a", record, {}, keys=keys, transport=unreachable, now=T0)
    assert status is None
    assert load_push_state().subscriptions["dev_a"].failures == 1


def test_an_answer_about_a_replaced_endpoint_changes_nothing(isolated_home: Path) -> None:
    old, new = Browser(f"{FCM}old"), Browser(f"{FCM}new")
    push_subscribe_device("dev_a", new.record(), {"dev_a"})
    push_record_outcome("dev_a", old.endpoint, 410)
    assert load_push_state().subscriptions["dev_a"].endpoint == new.endpoint


def test_a_too_large_answer_is_logged_as_our_bug(
    isolated_home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    push_subscribe_device("dev_a", Browser(f"{FCM}x").record(), {"dev_a"})
    with caplog.at_level("ERROR", logger=remote_push.__name__):
        push_record_outcome("dev_a", f"{FCM}x", 413)
    assert any("too large" in record.getMessage() for record in caplog.records)
    assert "dev_a" in load_push_state().subscriptions


# --- the public URL (SPEC §5.8, §5.10 item 9) -------------------------------------------------


TUNNELS = {
    "tunnels": [
        {"public_url": "http://abcd-12.ngrok-free.app", "proto": "http",
         "config": {"addr": "http://localhost:8750"}},
        {"public_url": "https://other.ngrok-free.app", "proto": "https",
         "config": {"addr": "http://localhost:18750"}},
        {"public_url": "https://abcd-12.ngrok-free.app", "proto": "https",
         "config": {"addr": "http://localhost:8750"}},
    ]
}  # fmt: skip


def test_discovery_picks_ngroks_https_tunnel_to_this_port() -> None:
    asked: list[tuple[str, float]] = []

    def agent_api(url: str, timeout: float) -> bytes:
        asked.append((url, timeout))
        return json.dumps(TUNNELS).encode()

    assert discover_ngrok_public_url(8750, fetch=agent_api) == "https://abcd-12.ngrok-free.app"
    assert asked == [("http://127.0.0.1:4040/api/tunnels", 1.0)], "loopback, one second"
    assert discover_ngrok_public_url(18750, fetch=agent_api) == "https://other.ngrok-free.app"
    assert discover_ngrok_public_url(750, fetch=agent_api) is None, "':750' is not ':8750'"


@pytest.mark.parametrize(
    "answer",
    [b"<html>ngrok is not running</html>", b"[]", b'{"tunnels": "none"}', b'{"tunnels": [1]}'],
)
def test_discovery_says_none_for_anything_but_a_tunnel_list(answer: bytes) -> None:
    assert discover_ngrok_public_url(8750, fetch=lambda url, timeout: answer) is None


def test_discovery_without_ngrok_running_says_none() -> None:
    def refused(url: str, timeout: float) -> bytes:
        raise ConnectionRefusedError("nothing listens on 4040")

    assert discover_ngrok_public_url(8750, fetch=refused) is None


# --- the routes (SPEC §5.8, §5.10 items 9, 10) ------------------------------------------------


def _sources() -> Sources:
    return Sources(
        projects=lambda: [],
        fleet=lambda project: {},
        board=lambda project: {},
        tasks=lambda project: [],
        memory=lambda project: [],
        panes=lambda agent, project, history: {"rows": [], "width": 0, "height": 0},
        explainability=lambda agent, project: {"available": False},
    )


@pytest.fixture
def runtime(isolated_home: Path) -> Runtime:
    return make_runtime()


@pytest.fixture
def app(runtime: Runtime, tmp_path: Path) -> Any:
    return build_app(runtime, sources=_sources(), dist_dir=tmp_path)


def unlocked(app: Any, runtime: Runtime, roster: set[str], **kw: Any) -> tuple[TestClient, str]:
    """A client with a device, which the roster then holds; and that device's id."""
    client = make_client(app, **kw)
    assert unlock(client, runtime).status_code == 200
    rows = client.get(f"{base(runtime)}/api/devices").json()
    device = next(row["id"] for row in rows if row["current"])
    roster.add(device)
    return client, device


def opt_in(client: TestClient, runtime: Runtime, browser: Browser) -> int:
    """What the page does on the tap that turns notifications on (SPEC §6.5): read the
    server's key, subscribe the browser to it, post the subscription. Its status."""
    status = client.get(f"{base(runtime)}/api/push").json()
    assert status["supported"] is True and status["vapid_public_key"]
    return client.post(
        f"{base(runtime)}/api/push/subscribe", json=browser.subscription()
    ).status_code


def audited(endpoint: str) -> list[str]:
    lines = remote_audit_path().read_text(encoding="utf-8").splitlines()
    return [line.split(" ", 3)[3] for line in lines if line.split(" ")[2] == endpoint]


def test_get_push_makes_the_key_once_and_says_whether_this_device_subscribed(
    app: Any, runtime: Runtime, roster: set[str]
) -> None:
    client, _device = unlocked(app, runtime, roster)
    first = client.get(f"{base(runtime)}/api/push")
    assert first.status_code == 200, first.text
    key = first.json()["vapid_public_key"]
    assert first.json() == {"supported": True, "vapid_public_key": key, "subscribed": False}
    stored = load_push_state().vapid
    assert stored is not None and stored.public_key == key
    browser = Browser(f"{FCM}phone")
    subscribed = client.post(f"{base(runtime)}/api/push/subscribe", json=browser.subscription())
    assert subscribed.status_code == 201 and subscribed.json() == {"subscribed": True}
    again = client.get(f"{base(runtime)}/api/push").json()
    assert again == {"supported": True, "vapid_public_key": key, "subscribed": True}


def test_a_subscribe_before_any_get_makes_the_key_too(
    app: Any, runtime: Runtime, roster: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The key is made on the first GET or subscribe (SPEC §5.2), so a subscription is never
    stored with no key to sign its pushes, which would send nothing, and say nothing."""
    transport = Transport()
    monkeypatch.setattr(remote_push, "push_https_transport", transport)
    client, _device = unlocked(app, runtime, roster)
    phone = Browser(f"{FCM}phone")
    assert (
        client.post(f"{base(runtime)}/api/push/subscribe", json=phone.subscription()).status_code
        == 201
    )
    assert load_push_state().vapid is not None
    assert client.post(f"{base(runtime)}/api/push/test").status_code == 202
    transport.wait_for(1)
    assert phone.read(transport.sent[0][2])["title"] == TEST_TITLE


def test_subscribing_is_not_a_write_and_is_audited_by_host(
    app: Any, runtime: Runtime, roster: set[str]
) -> None:
    """A read-only phone must still hear that something needs it (SPEC App. B.3)."""
    client, device = unlocked(app, runtime, roster)
    assert runtime.allow_write is False
    browser = Browser(f"{FCM}phone")
    response = client.post(f"{base(runtime)}/api/push/subscribe", json=browser.subscription())
    assert response.status_code == 201, response.text
    assert load_push_state().subscriptions[device].endpoint == browser.endpoint
    assert audited("push/subscribe") == ["fcm.googleapis.com"]


@pytest.mark.parametrize(
    ("change", "status", "error"),
    [
        ({"endpoint": "https://evil.example/push"}, 400, "push_host_not_allowed"),
        ({"endpoint": "http://fcm.googleapis.com/fcm/send/x"}, 400, "push_host_not_allowed"),
        ({"endpoint": FCM + "x" * 1024}, 413, "too_large"),
        ({"endpoint": None}, 400, "invalid"),
        ({"keys": None}, 400, "invalid"),
        ({"keys": {"p256dh": _b64(b"\x04" + b"\x01" * 64), "auth": _b64(b"a" * 16)}}, 400,
         "invalid"),  # off the curve
        ({"keys": {"p256dh": _b64(b"\x04" * 33), "auth": _b64(b"a" * 16)}}, 400, "invalid"),
        ({"keys": {"p256dh": "not base64!", "auth": _b64(b"a" * 16)}}, 400, "invalid"),
    ],
)  # fmt: skip
def test_a_subscription_that_is_not_one_is_refused(
    app: Any,
    runtime: Runtime,
    roster: set[str],
    change: dict[str, Any],
    status: int,
    error: str,
) -> None:
    client, _device = unlocked(app, runtime, roster)
    body = {**Browser(f"{FCM}x").subscription(), **change}
    response = client.post(f"{base(runtime)}/api/push/subscribe", json=body)
    assert (response.status_code, response.json()["error"]) == (status, error), response.text
    assert load_push_state().subscriptions == {}


def test_a_short_auth_secret_is_refused(app: Any, runtime: Runtime, roster: set[str]) -> None:
    client, _device = unlocked(app, runtime, roster)
    body = Browser(f"{FCM}x").subscription()
    body["keys"]["auth"] = _b64(b"a" * 15)
    response = client.post(f"{base(runtime)}/api/push/subscribe", json=body)
    assert (response.status_code, response.json()["error"]) == (400, "invalid")


def test_deleting_the_subscription_forgets_it(app: Any, runtime: Runtime, roster: set[str]) -> None:
    client, device = unlocked(app, runtime, roster)
    client.post(f"{base(runtime)}/api/push/subscribe", json=Browser(f"{FCM}x").subscription())
    response = client.delete(f"{base(runtime)}/api/push/subscription")
    assert response.status_code == 200 and response.json() == {"subscribed": False}
    assert device not in load_push_state().subscriptions
    assert client.get(f"{base(runtime)}/api/push").json()["subscribed"] is False
    assert audited("push/subscription") == ["-"]


def test_a_test_push_goes_to_this_device_only_once_in_ten_seconds(
    app: Any, runtime: Runtime, roster: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = Transport()
    monkeypatch.setattr(remote_push, "push_https_transport", transport)
    client, device = unlocked(app, runtime, roster)
    other, other_device = unlocked(app, runtime, roster)
    assert client.post(f"{base(runtime)}/api/push/test").status_code == 404
    mine, theirs = Browser(f"{FCM}mine"), Browser(f"{FCM}theirs")
    assert opt_in(client, runtime, mine) == opt_in(other, runtime, theirs) == 201
    queued = client.post(f"{base(runtime)}/api/push/test")
    assert queued.status_code == 202 and queued.json() == {"queued": True}
    transport.wait_for(1)
    ((endpoint, _headers, body),) = transport.sent
    assert endpoint == mine.endpoint
    payload = mine.read(body)
    assert payload["title"] == TEST_TITLE and payload["url"] is None
    throttled = client.post(f"{base(runtime)}/api/push/test")
    assert throttled.status_code == 429
    assert throttled.json()["error"] == "push_test_throttled"
    assert 1 <= int(throttled.headers["retry-after"]) <= 10
    assert audited("push/test") == ["-"]
    monkeypatch.setattr(remote_push, "PUSH_TEST_INTERVAL_SECONDS", 0.0)
    assert client.post(f"{base(runtime)}/api/push/test").status_code == 202
    transport.wait_for(2)
    assert [endpoint for endpoint, _headers, _body in transport.sent] == [mine.endpoint] * 2
    assert set(load_push_state().subscriptions) == {device, other_device}


def test_the_test_push_says_not_subscribed_for_a_device_without_one(
    app: Any, runtime: Runtime, roster: set[str]
) -> None:
    client, _device = unlocked(app, runtime, roster)
    response = client.post(f"{base(runtime)}/api/push/test")
    assert (response.status_code, response.json()["error"]) == (404, "not_subscribed")


def test_without_cryptography_push_says_how_to_get_it_and_nothing_else_breaks(
    app: Any, runtime: Runtime, roster: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _device = unlocked(app, runtime, roster)
    monkeypatch.setitem(sys.modules, "cryptography", None)
    assert client.get(f"{base(runtime)}/api/push").json() == {
        "supported": False,
        "reason": PUSH_INSTALL_HINT,
        "vapid_public_key": None,
        "subscribed": False,
    }
    body = Browser(f"{FCM}x").subscription()
    refused = client.post(f"{base(runtime)}/api/push/subscribe", json=body)
    assert (refused.status_code, refused.json()["error"]) == (503, "push_unavailable")
    assert client.post(f"{base(runtime)}/api/push/test").status_code == 503
    assert client.get(f"{base(runtime)}/api/remote").status_code == 200
    assert start_push_sender(RemoteKit(runtime)) is None, "the sender never starts"


def test_no_request_header_reaches_a_push_link(
    app: Any, runtime: Runtime, roster: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A forged ``Host`` plus ``X-Forwarded-Proto: https`` would make a push link open a page
    the forger serves, where the human types the passphrase, if any header were read."""
    transport = Transport()
    monkeypatch.setattr(remote_push, "push_https_transport", transport)
    forged = {
        "host": "evil.ngrok-free.app",
        "x-forwarded-proto": "https",
        "x-forwarded-host": "evil.ngrok-free.app",
        "forwarded": "host=evil.ngrok-free.app;proto=https",
        "origin": "https://evil.ngrok-free.app",
    }
    client, _device = unlocked(app, runtime, roster, base_url="https://testserver", headers=forged)
    phone = Browser(f"{FCM}phone")
    assert opt_in(client, runtime, phone) == 201
    assert client.post(f"{base(runtime)}/api/push/test").status_code == 202
    transport.wait_for(1)
    assert phone.read(transport.sent[0][2])["url"] is None
    assert app.kit.kit_public_url() is None


# --- with the real device model (lane b-security) ---------------------------------------------


@pytest.mark.xfail(strict=True, reason="needs lane b-security: Runtime.device_ids")
def test_with_the_real_roster_a_revoked_devices_subscription_is_dropped(
    app: Any, runtime: Runtime
) -> None:
    client, other = make_client(app), make_client(app)
    assert unlock(client, runtime).status_code == 200
    assert unlock(other, runtime).status_code == 200
    rows = client.get(f"{base(runtime)}/api/devices").json()
    mine = next(row["id"] for row in rows if row["current"])
    theirs = next(row["id"] for row in rows if not row["current"])
    for who, endpoint in ((client, "mine"), (other, "theirs")):
        assert opt_in(who, runtime, Browser(f"{FCM}{endpoint}")) == 201
    runtime.set_allow_write(True)
    assert client.delete(f"{base(runtime)}/api/devices/{theirs}").status_code == 200
    assert opt_in(client, runtime, Browser(f"{FCM}mine-again")) == 201
    assert set(load_push_state().subscriptions) == {mine}


# --- ngrok: the static domain and the old-ngrok hint (SPEC §5.8, §5.10 item 12) ---------------


def _recording_popen(spawned: list[list[str]]) -> Callable[..., subprocess.Popen[str]]:
    def popen(command: list[str], **kwargs: Any) -> subprocess.Popen[str]:
        spawned.append(list(command))
        return subprocess.Popen([sys.executable, "-c", "pass"], **kwargs)

    return popen


def test_ngrok_is_told_the_static_domain_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AISQUARE_REMOTE_NGROK_URL", "https://remote-anmol.ngrok-free.app/")
    spawned: list[list[str]] = []
    tunnel = NgrokTunnel(
        8750, which=lambda _name: "/usr/bin/ngrok", popen=_recording_popen(spawned)
    )
    assert tunnel.start_tunnel() is None
    tunnel.wait_for_url(5)
    tunnel.stop_tunnel()
    assert spawned == [
        ["ngrok", "http", "8750", "--log=stdout", "--log-format=json",
         "--url=remote-anmol.ngrok-free.app"]
    ]  # fmt: skip


def test_without_a_static_domain_ngrok_picks_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AISQUARE_REMOTE_NGROK_URL", raising=False)
    spawned: list[list[str]] = []
    tunnel = NgrokTunnel(
        8750, which=lambda _name: "/usr/bin/ngrok", popen=_recording_popen(spawned)
    )
    assert tunnel.start_tunnel() is None
    tunnel.wait_for_url(5)
    tunnel.stop_tunnel()
    assert spawned == [ngrok_command(8750)]
    assert tunnel.static_host is None


@pytest.mark.parametrize(
    ("written", "host"),
    [
        ("remote-anmol.ngrok-free.app", "remote-anmol.ngrok-free.app"),
        ("https://remote-anmol.ngrok-free.app", "remote-anmol.ngrok-free.app"),
        ("https://Remote-Anmol.ngrok-free.app/r/x/", "remote-anmol.ngrok-free.app"),
        ("  remote-anmol.ngrok-free.app/  ", "remote-anmol.ngrok-free.app"),
        ("", None),
        ("   ", None),
        (None, None),
    ],
)
def test_a_static_domain_is_read_however_it_was_written(
    written: str | None, host: str | None
) -> None:
    assert ngrok_static_host(written) == host


def test_an_ngrok_too_old_for_url_says_to_update_it(tmp_path: Path) -> None:
    assert parse_log_line("ERROR:  unknown flag: --url\n").error == TOO_OLD_HINT
    assert parse_log_line("Incorrect Usage. flag provided but not defined: -url").error == (
        TOO_OLD_HINT
    )
    assert parse_log_line("ERROR:  unknown flag: --log-format").error is None, "not ours"
    assert parse_log_line("t=2026 lvl=info msg=hello").error is None
    script = tmp_path / "old-ngrok.py"
    script.write_text(
        "import sys\nprint('ERROR:  unknown flag: --url', file=sys.stderr)\nsys.exit(1)\n"
    )
    tunnel = NgrokTunnel(8750, command=[sys.executable, str(script)])
    assert tunnel.start_tunnel() is None
    assert tunnel.wait_for_url(10) is None
    assert tunnel.error == TOO_OLD_HINT
    tunnel.stop_tunnel()
