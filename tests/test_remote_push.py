"""Web Push (SPEC §5): the crypto, the allowlist, the file, the sender's rules, the routes, the TUI.

No test here reaches the network. The sender and the one-shot pushes take an
injected transport, and an autouse guard stands in for the real one and for any
plain HTTP connection, so a test that forgot to inject fails instead of POSTing
to a push service. Wherever a rule is about time (the coalescing window, the
per-device throttle, the system pushes), the clock is a fake one and the
sender's state machine is driven directly, one scan and one tick at a time.

Each push is DECRYPTED here, by a receiver written against RFC 8291 §3.4 with
``cryptography``'s own HKDF rather than the module's hand-rolled HMACs, so a
test reads exactly what a phone would.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import http.client
import json
import os
import stat
import struct
import subprocess
import sys
import threading
import time
import unicodedata
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from starlette.testclient import TestClient

from aisquare.cli.ui.remote_control import RemoteController
from aisquare.core.paths import remote_audit_path, remote_push_path
from aisquare.services import remote_push, remote_server
from aisquare.services.ngrok_tunnel import (
    TOO_OLD_HINT,
    NgrokTunnel,
    ngrok_command,
    ngrok_static_host,
    parse_log_line,
)
from aisquare.services.remote_needs import NeedsItem
from aisquare.services.remote_push import (
    AUTO_OFF_BODY,
    AUTO_OFF_READ_ONLY_BODY,
    AUTO_OFF_TITLE,
    EXPIRY_TITLE,
    FAREWELL_TITLE,
    LOCKOUT_TITLE,
    PUSH_INSTALL_HINT,
    TEST_TITLE,
    PushSubscriptionRecord,
    RemotePushSender,
    encrypt_push_payload,
    load_or_create_vapid_keys,
    load_push_state,
    push_drain,
    push_farewell,
    push_host_allowed,
    push_record_outcome,
    push_security_alert,
    push_send_one,
    push_subscribe_device,
    push_subscription_from_body,
    push_unsubscribe_device,
    start_push_sender,
    vapid_authorization,
)
from aisquare.services.remote_server import RemoteKit, Runtime, Sources, build_app
from tests.remote_kit_helpers import base, make_client, make_runtime, unlock
from tests.test_remote_control import FakeServer

REAL_TRANSPORT = remote_push.push_https_transport
"""Kept before the guard below replaces it, for the one test of the real transport."""

T0 = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)
FCM = "https://fcm.googleapis.com/fcm/send/"
PUBLIC_ORIGIN = "https://abcd-12.ngrok-free.app"


# --- guards -----------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def no_push_service(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """The real transport and any plain HTTP connection (ngrok's local API once was one),
    replaced by refusals the test fails on: a test that forgot to hand in its own transport
    is red, never a request on the wire."""
    reached: list[str] = []

    def refuse_push(endpoint: str, headers: dict[str, str], body: bytes) -> int:
        reached.append(endpoint)
        raise OSError("a test reached the real push transport")

    class RefusedConnection:
        def __init__(self, host: str, port: int | None = None, **_options: object) -> None:
            reached.append(f"http://{host}:{port}")
            raise OSError("a test opened an HTTP connection")

    monkeypatch.setattr(remote_push, "push_https_transport", refuse_push)
    monkeypatch.setattr(http.client, "HTTPConnection", RefusedConnection)
    yield reached
    assert reached == [], f"a test reached the network: {reached}"


@pytest.fixture
def roster(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    """The devices the runtime still has (signed in or signed out), as the test says.

    ``push_device_ids`` is the seam push reads ``Runtime.device_ids`` through, so the
    rules here are tested against a roster the test controls. The tests under "with the
    real device model" read the real one.
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


# --- the sender's world -----------------------------------------------------------------------


class Clock:
    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> datetime:
        self.now += timedelta(seconds=seconds)
        return self.now


class Watcher:
    """The needs watcher, as far as the sender reads it: the latest scan's items."""

    def __init__(self) -> None:
        self.items: list[NeedsItem] = []

    def needs_items_now(self) -> list[NeedsItem]:
        return list(self.items)


def needs_item(
    n: int,
    *,
    kind: str = "question",
    agent: str | None = "coder-auth",
    project_name: str = "aisquare-cli",
    project_id: str = "prj_8c1e",
    push_after: datetime | None = T0,
    reason: str | None = None,
) -> NeedsItem:
    """One item, its excerpt and detail strings nothing a push may ever carry."""
    return NeedsItem(
        id=f"ny_{n:016x}",
        kind=kind,
        project_id=project_id,
        project_name=project_name,
        agent=agent,
        agent_id=None if agent is None else f"agt_{n}",
        reason=reason or f"{agent or project_name} asks you a question ({n})",
        excerpt="EXCERPT that never leaves the machine",
        detail={"text": "DETAIL that never leaves the machine"},
        answers=(),
        since=T0,
        actions=("answer", "open", "dismiss"),
        push_after=push_after,
    )


DEVICES = ("dev_aaaaaaaa", "dev_bbbbbbbb")


@dataclass
class World:
    """One server's push lane, two subscribed devices, a fake watcher and a fake clock."""

    kit: RemoteKit
    sender: RemotePushSender
    watcher: Watcher
    transport: Transport
    clock: Clock
    roster: set[str]
    browsers: dict[str, Browser]

    def scan(self, *items: NeedsItem) -> None:
        """One needs scan: the watcher's feed becomes ``items``, the sender hears of it."""
        self.watcher.items = list(items)
        self.sender.push_scan_seen(list(items), self.clock.now)
        self.sender.push_run_due()

    def later(self, seconds: float) -> None:
        self.clock.advance(seconds)
        self.sender.push_run_due()

    def pushes(self) -> list[tuple[str, dict[str, Any]]]:
        """``(device id, decrypted payload)`` for every push sent, in order."""
        by_endpoint = {browser.endpoint: d for d, browser in self.browsers.items()}
        return [
            (by_endpoint[endpoint], self.browsers[by_endpoint[endpoint]].read(body))
            for endpoint, _headers, body in self.transport.sent
        ]

    def titles(self) -> list[tuple[str, str]]:
        return [(device, payload["title"]) for device, payload in self.pushes()]


@pytest.fixture
def world(runtime: Runtime, roster: set[str]) -> World:
    kit = RemoteKit(runtime)
    watcher = Watcher()
    kit.lane_state["needs"] = watcher
    clock, transport = Clock(), Transport()
    roster.update(DEVICES)
    load_or_create_vapid_keys()
    browsers = {device: Browser(f"{FCM}{device}") for device in DEVICES}
    for device, browser in browsers.items():
        push_subscribe_device(device, browser.record(), roster)
    sender = RemotePushSender(kit, transport=transport, clock=clock)
    sender.push_run_due()  # the first tick: the system checks, with nothing to say
    return World(kit, sender, watcher, transport, clock, roster, browsers)


def every_device(title: str) -> list[tuple[str, str]]:
    return [(device, title) for device in DEVICES]


def auto_off_title(minutes: int) -> str:
    return AUTO_OFF_TITLE.format(minutes=minutes)


def push_item(world: World, n: int) -> Any:
    """Item ``n`` through two scans and its window, then the throttle's 20 s: the link its
    notification carried, or ``None``."""
    world.scan(needs_item(n))
    world.scan(needs_item(n))
    world.later(5)
    link = world.pushes()[-1][1]["url"]
    world.later(20)
    return link


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


def test_what_was_pushed_is_kept_a_week() -> None:
    """``pushed`` is what keeps a restart from pushing an item twice; past a week its id
    will not come back, and the file would keep it for good."""
    now = T0 + timedelta(days=30)
    kept = remote_push._push_prune_pushed(
        {
            "ny_old": (now - timedelta(days=8)).isoformat(),
            "ny_new": (now - timedelta(hours=1)).isoformat(),
            "ny_unreadable": "not a time",
        },
        now,
    )
    assert set(kept) == {"ny_new"}


def test_at_most_the_newest_1000_pushed_ids_are_kept() -> None:
    now = T0 + timedelta(days=30)
    pushed = {f"ny_{n:016x}": (now - timedelta(minutes=n)).isoformat() for n in range(1005)}
    kept = remote_push._push_prune_pushed(pushed, now)
    assert len(kept) == 1000
    assert f"ny_{0:016x}" in kept and f"ny_{1004:016x}" not in kept, "the oldest go first"


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


def test_the_subscription_a_device_has_sent_again_is_no_change(isolated_home: Path) -> None:
    """The page sends its subscription again after every unlock. The record stays as it was,
    its failures in a row included; a new subscription, or one moving here, is a change."""
    phone, other = Browser(f"{FCM}phone"), Browser(f"{FCM}other")
    assert push_subscribe_device("dev_a", phone.record(), {"dev_a"}) is True
    push_record_outcome("dev_a", phone.endpoint, 403)
    kept = load_push_state().subscriptions["dev_a"]
    again = push_subscription_from_body(phone.subscription(), now=T0 + timedelta(hours=1))
    assert push_subscribe_device("dev_a", again, {"dev_a"}) is False
    assert load_push_state().subscriptions["dev_a"] == kept and kept.failures == 1
    assert push_subscribe_device("dev_a", other.record(), {"dev_a"}) is True
    assert push_subscribe_device("dev_b", other.record(), {"dev_a", "dev_b"}) is True
    assert set(load_push_state().subscriptions) == {"dev_b"}


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


@pytest.mark.parametrize(
    ("statuses", "kept", "refusals"),
    [
        ((None, 503, 401), True, 1),
        ((429, 429, 403), True, 1),
        ((403, 403, None, 403), True, 1),
        ((403, 503, 403, 403), True, 2),
        ((403, 503, 403, 403, 403), False, None),
        ((403, 413, 403, 403), True, 2),
    ],
)
def test_only_refusals_in_a_row_drop_a_subscription(
    isolated_home: Path, statuses: tuple[int | None, ...], kept: bool, refusals: int | None
) -> None:
    """A laptop that wakes with no network times out twice, and the service then refuses
    once, around a clock correction: one refusal, not the third in a row. A timeout, a 429,
    a 5xx or a 413 between refusals ends their row; each but the 413 still counts as a
    failure."""
    browser = Browser(f"{FCM}x")
    push_subscribe_device("dev_a", browser.record(), {"dev_a"})
    for status in statuses:
        push_record_outcome("dev_a", browser.endpoint, status)
    held = load_push_state().subscriptions.get("dev_a")
    assert (held is not None) is kept
    if held is not None:
        assert held.refusals == refusals
        assert held.failures == sum(status != 413 for status in statuses)


def test_the_refusals_in_a_row_are_kept_in_the_file_and_a_success_ends_them(
    isolated_home: Path,
) -> None:
    browser = Browser(f"{FCM}x")
    push_subscribe_device("dev_a", browser.record(), {"dev_a"})
    push_record_outcome("dev_a", browser.endpoint, 403)
    push_record_outcome("dev_a", browser.endpoint, 403)
    assert load_push_state().subscriptions["dev_a"].refusals == 2  # read back from the file
    push_record_outcome("dev_a", browser.endpoint, 201)
    push_record_outcome("dev_a", browser.endpoint, 403)
    push_record_outcome("dev_a", browser.endpoint, 403)
    held = load_push_state().subscriptions["dev_a"]
    assert (held.failures, held.refusals) == (2, 2)


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


def test_the_request_carries_the_headers_the_push_services_require(world: World) -> None:
    world.scan(needs_item(1))
    world.scan(needs_item(1))
    world.later(5)
    endpoint, headers, _body = world.transport.sent[0]
    assert headers["Content-Encoding"] == "aes128gcm"
    assert headers["Content-Type"] == "application/octet-stream"
    assert headers["TTL"] == "3600" and headers["Urgency"] == "high"
    assert headers["Topic"] == "asq-needs"
    assert headers["Authorization"].startswith("vapid t=")
    assert endpoint == world.browsers[DEVICES[0]].endpoint


def test_a_key_made_again_while_the_server_runs_signs_the_next_push(world: World) -> None:
    """``remote-push.json`` lost while the server runs: the next ``GET api/push`` makes a new
    key, and the phones subscribe again against it. A sender that kept the old key signed
    with it, and every push service refused the phones until it had dropped them all."""

    def signed_with() -> str:
        return world.transport.sent[-1][1]["Authorization"].rsplit("k=", 1)[1]

    first = load_push_state().vapid
    assert first is not None
    push_item(world, 1)
    assert signed_with() == first.public_key
    remote_push_path().unlink()
    second = load_or_create_vapid_keys()
    for device, browser in world.browsers.items():
        push_subscribe_device(device, browser.record(), world.roster)
    push_item(world, 2)
    assert second.public_key != first.public_key
    assert signed_with() == second.public_key


# --- when a needs item is pushed (SPEC §5.6, §5.10 items 6, 7, 11) ----------------------------


def test_an_item_seen_in_one_scan_only_is_never_pushed(world: World) -> None:
    world.scan(needs_item(1))
    world.later(30)
    world.scan()
    world.later(60)
    assert world.transport.sent == []


def test_an_item_in_two_scans_is_pushed_once_when_its_window_closes(world: World) -> None:
    item = needs_item(1)
    world.scan(item)
    world.later(3)
    world.scan(item)
    world.later(4.9)
    assert world.transport.sent == [], "the coalescing window is still open"
    world.later(0.1)
    assert world.titles() == every_device("aisquare-cli: coder-auth needs you")
    for _ in range(10):
        world.later(3)
        world.scan(item)
    assert len(world.transport.sent) == 2, "once per item, however long it stays"


def test_an_item_gone_inside_the_window_is_not_pushed(world: World) -> None:
    item = needs_item(1)
    world.scan(item)
    world.scan(item)
    world.later(2)
    world.scan()
    world.later(5)
    assert world.transport.sent == []


def test_an_item_the_feed_dropped_before_its_window_closed_is_not_pushed(world: World) -> None:
    """Dismissed on the page, or cleared, between two scans: the watcher's feed, not the
    last scan the sender heard of, is what the window's close reads (SPEC §5.6 step 4)."""
    item = needs_item(1, kind="lost")
    world.scan(item)
    world.scan(item)
    world.watcher.items = []  # the feed lost it; no scan has told the sender yet
    world.later(5)
    assert world.transport.sent == []


def test_with_nobody_subscribed_an_item_the_feed_dropped_is_not_counted_as_pushed(
    world: World,
) -> None:
    """With nobody subscribed, the window's close is where its items are recorded as
    pushed, so one the feed had already dropped must not be: when the same thing happens
    again (the same agent's pane lost twice is one id) and a phone has subscribed since,
    that time is pushed."""
    for device in DEVICES:
        push_unsubscribe_device(device, world.roster)
    item = needs_item(1, kind="lost")
    world.scan(item)
    world.scan(item)
    world.watcher.items = []  # the feed lost it; no scan has told the sender yet
    world.later(5)
    assert item.id not in load_push_state().pushed
    for device, browser in world.browsers.items():
        push_subscribe_device(device, browser.record(), world.roster)
    world.scan()
    world.scan(item)
    world.scan(item)
    world.later(5)
    assert world.titles() == every_device("aisquare-cli: coder-auth needs you")


def test_an_item_back_inside_the_window_starts_over(world: World) -> None:
    """Gone for one scan is gone: back, it needs two consecutive scans again."""
    item = needs_item(1)
    world.scan(item)
    world.scan(item)
    world.scan()
    world.scan(item)
    world.later(5)
    assert world.transport.sent == [], "one scan since it came back"
    world.scan(item)
    world.later(5)
    assert len(world.transport.sent) == 2


def test_an_item_whose_push_after_is_ahead_is_pushed_after_it_once(world: World) -> None:
    item = needs_item(1, push_after=T0 + timedelta(minutes=5))
    while world.clock.now < T0 + timedelta(minutes=5) - timedelta(seconds=3):
        world.scan(item)
        world.later(3)
        assert world.transport.sent == [], world.clock.now
    for _ in range(5):
        world.scan(item)
        world.later(3)
    assert world.titles() == every_device("aisquare-cli: coder-auth needs you")


def test_a_feed_only_item_is_never_pushed(world: World) -> None:
    item = needs_item(1, push_after=None)
    for _ in range(20):
        world.scan(item)
        world.later(3)
    assert world.transport.sent == []


def test_without_a_needs_watcher_nothing_is_pushed(world: World) -> None:
    del world.kit.lane_state["needs"]
    item = needs_item(1)
    world.sender.push_scan_seen([item], world.clock.now)
    world.sender.push_scan_seen([item], world.clock.now)
    world.later(5)
    assert world.transport.sent == []


def test_a_burst_is_one_notification_per_device(world: World) -> None:
    first, second = needs_item(1), needs_item(2, agent="coder-db")
    world.scan(first)
    world.scan(first, second)
    world.later(1)
    world.scan(first, second)
    world.later(5)
    pushes = world.pushes()
    assert [device for device, _payload in pushes] == list(DEVICES)
    payload = pushes[0][1]
    assert payload["title"] == "2 things need you"
    assert payload["body"] == f"{first.reason} · {second.reason}"
    assert payload["ids"] == [first.id, second.id]


def test_one_device_hears_at_most_once_in_20_seconds(world: World) -> None:
    first, second = needs_item(1), needs_item(2, agent="coder-db")
    world.scan(first)
    world.scan(first)
    world.later(5)  # T0 + 5 s: the first push
    assert len(world.transport.sent) == 2
    world.scan(first, second)
    world.later(3)
    world.scan(first, second)  # second is pushable: its window closes at T0 + 13 s
    world.later(5)
    assert len(world.transport.sent) == 2, "8 s after the first push: owed, not sent"
    world.later(11.9)
    assert len(world.transport.sent) == 2, "19.9 s after it"
    world.later(0.1)
    assert world.titles()[2:] == every_device("aisquare-cli: coder-db needs you · 2 open")


def test_what_cleared_while_the_throttle_ran_is_not_pushed(world: World) -> None:
    first, second = needs_item(1), needs_item(2, agent="coder-db")
    world.scan(first)
    world.scan(first)
    world.later(5)
    world.scan(first, second)
    world.scan(first, second)
    world.later(5)  # second's window closed; the devices are owed it for 10 s more
    world.scan(first)  # and it cleared
    world.later(15)
    assert len(world.transport.sent) == 2


def test_the_title_counts_what_is_open_beyond_the_push(world: World) -> None:
    pushable = needs_item(1)
    feed_only = [needs_item(2, push_after=None), needs_item(3, push_after=None)]
    world.scan(pushable, *feed_only)
    world.scan(pushable, *feed_only)
    world.later(5)
    assert world.titles() == every_device("aisquare-cli: coder-auth needs you · 3 open")


def test_a_notification_names_at_most_50_items(world: World) -> None:
    """A fleet on fire is one notification, its payload in budget: the page reads the feed."""
    items = [needs_item(n) for n in range(60)]
    message = remote_push.push_needs_message(items, total=60, base_url=None)
    assert message["ids"] == [item.id for item in items[:50]]


def test_a_project_level_item_is_titled_by_its_project(world: World) -> None:
    item = needs_item(1, kind="fleet_down", agent=None, reason="tmux is not answering")
    world.scan(item)
    world.scan(item)
    world.later(5)
    assert world.titles() == every_device("aisquare-cli needs you")


def test_the_payload_says_only_the_title_the_reason_and_where_to_go(world: World) -> None:
    item = needs_item(1)
    world.scan(item)
    world.scan(item)
    world.later(5)
    for _device, payload in world.pushes():
        assert set(payload) == {"v", "title", "body", "tag", "url", "ids"}
        assert payload["body"] == item.reason and payload["tag"] == "asq-needs"
    browsers = {browser.endpoint: browser for browser in world.browsers.values()}
    for endpoint, _headers, body in world.transport.sent:
        browser = browsers[endpoint]
        plaintext = read_push(body, browser.private, browser.auth)
        assert b"EXCERPT" not in plaintext and b"DETAIL" not in plaintext


def test_a_hostile_role_reaches_the_title_as_at_most_40_printable_characters(
    world: World,
) -> None:
    """A role is free text an agent sets, and a board item names its author by it."""
    role = ("‮\x1b[31mmanager\n" + "x" * 300)[:300]
    item = needs_item(1, kind="board_question", agent=role, reason=f"{role} asks on the board")
    world.scan(item)
    world.scan(item)
    world.later(5)
    title = world.pushes()[0][1]["title"]
    assert title.startswith("aisquare-cli: ") and title.endswith(" needs you")
    name = title.removeprefix("aisquare-cli: ").removesuffix(" needs you")
    assert len(name) <= 40 and name.isprintable()
    assert not any(unicodedata.category(ch) in {"Cc", "Cf"} for ch in title)
    body = world.pushes()[0][1]["body"]
    assert len(body) <= 160 and body.isprintable()


def test_card_links_lead_to_the_item_its_project_or_the_feed(world: World) -> None:
    world.kit.runtime.note_public_origin(PUBLIC_ORIGIN)
    link = f"{PUBLIC_ORIGIN}/r/{world.kit.runtime.token}/"
    agent_item = needs_item(1)
    world.scan(agent_item)
    world.scan(agent_item)
    world.later(5)
    project_item = needs_item(2, kind="crashed", agent=None)
    world.scan(agent_item, project_item)
    world.scan(agent_item, project_item)
    world.later(20)
    three, four = needs_item(3), needs_item(4, agent="coder-db")
    world.scan(agent_item, project_item, three, four)
    world.scan(agent_item, project_item, three, four)
    world.later(20)
    urls = [payload["url"] for device, payload in world.pushes() if device == DEVICES[0]]
    assert urls == [
        f"{link}#/n/{agent_item.id}/p/prj_8c1e/a/coder-auth",
        f"{link}#/n/{project_item.id}/p/prj_8c1e",
        f"{link}#/",
    ]


def test_a_card_link_leaves_out_what_the_pages_router_would_refuse(world: World) -> None:
    world.kit.runtime.note_public_origin(PUBLIC_ORIGIN)
    item = needs_item(1, kind="board_question", agent="the release manager")
    world.scan(item)
    world.scan(item)
    world.later(5)
    url = world.pushes()[0][1]["url"]
    assert url.endswith(f"#/n/{item.id}/p/prj_8c1e"), url


def test_without_an_authoritative_origin_the_link_is_null(world: World) -> None:
    item = needs_item(1)
    world.scan(item)
    world.scan(item)
    world.later(5)
    assert [payload["url"] for _device, payload in world.pushes()] == [None, None]


def test_a_restart_with_what_was_pushed_on_file_pushes_none_of_it(world: World) -> None:
    item = needs_item(1)
    world.scan(item)
    world.scan(item)
    world.later(5)
    assert len(world.transport.sent) == 2
    again = RemotePushSender(world.kit, transport=world.transport, clock=world.clock)
    for _ in range(3):
        world.watcher.items = [item]
        again.push_scan_seen([item], world.clock.now)
        world.clock.advance(3)
        again.push_run_due()
    assert len(world.transport.sent) == 2


def test_a_full_disk_costs_no_device_its_push_and_repeats_none(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neither what a push service said nor what was pushed can be written: every device
    still gets its notification, and nobody gets it again every 25 s."""

    def full_disk(state: remote_push.PushState) -> None:
        raise OSError(28, "No space left on device")

    item = needs_item(1)
    world.scan(item)
    world.scan(item)
    world.transport.statuses = [410]  # each answer means a write: drop the subscription
    monkeypatch.setattr(remote_push, "save_push_state", full_disk)
    world.later(5)
    assert [device for device, _payload in world.pushes()] == list(DEVICES)
    world.transport.statuses = [201]
    for _ in range(20):
        world.later(3)
        world.scan(item)
    assert len(world.transport.sent) == 2


def test_a_device_no_longer_live_is_neither_pushed_nor_kept(world: World) -> None:
    world.roster.discard(DEVICES[1])
    item = needs_item(1)
    world.scan(item)
    world.scan(item)
    world.later(5)
    assert [device for device, _payload in world.pushes()] == [DEVICES[0]]
    assert set(load_push_state().subscriptions) == {DEVICES[0]}


def test_with_nobody_subscribed_the_item_counts_as_pushed(world: World) -> None:
    """A phone that subscribes later is told what happens next, not what it can already
    see in the feed it is looking at."""
    for device in DEVICES:
        push_unsubscribe_device(device, world.roster)
    item = needs_item(1)
    world.scan(item)
    world.scan(item)
    world.later(5)
    for device, browser in world.browsers.items():
        push_subscribe_device(device, browser.record(), world.roster)
    world.scan(item)
    world.later(30)
    assert world.transport.sent == []
    assert item.id in load_push_state().pushed


# --- the public URL (SPEC §5.8, §5.10 item 9) -------------------------------------------------


def test_with_no_origin_announced_a_link_is_null_and_ngroks_api_is_never_asked(
    world: World, no_push_service: list[str]
) -> None:
    """``asq remote serve`` without ``--public-url`` asked ngrok's local API on
    127.0.0.1:4040 for its links' origin. Anyone on the machine can listen there before the
    human's ngrok does (which then moves to 4041) and name any https host: links led to
    ``https://<theirs>/r/<the real token>/``, the service worker opened them, and one tap
    handed over the token. Nothing is asked now, and the link is null: the notification
    opens the page the phone subscribed from."""
    for n in (1, 2):
        assert push_item(world, n) is None
        world.later(60)
    assert no_push_service == [], "no connection to anything, ngrok's API included"
    assert world.kit.kit_public_url() is None


def test_an_origin_the_tui_or_serve_announced_leads_every_link(world: World) -> None:
    world.kit.runtime.note_public_origin(PUBLIC_ORIGIN)
    for n in (1, 2, 3):
        assert push_item(world, n).startswith(f"{PUBLIC_ORIGIN}/r/{world.kit.runtime.token}/")
        world.later(60)


# --- system pushes (SPEC §5.6, §5.10 item 8) --------------------------------------------------


def test_the_auto_off_warning_goes_once_per_deadline_and_again_after_an_extension(
    world: World,
) -> None:
    world.kit.runtime.set_auto_off(T0 + timedelta(minutes=25))
    world.later(30)
    assert world.transport.sent == [], "25 minutes ahead: not yet"
    world.clock.advance(14 * 60)
    world.later(30)  # T0 + 15 min: the first check inside the ten minutes
    assert world.titles() == every_device(auto_off_title(10))
    for _ in range(4):
        world.later(30)
    assert len(world.transport.sent) == 2, "once per deadline"
    world.kit.runtime.set_auto_off(world.clock.now + timedelta(minutes=60))  # extended
    world.clock.advance(50 * 60)
    world.later(30)
    assert world.titles()[2:] == every_device(auto_off_title(10))
    assert world.pushes()[0][1]["tag"] == "asq-auto-off"


def test_the_auto_off_warning_offers_the_extension_only_while_writes_are_on(
    world: World,
) -> None:
    """Extending is a write (SPEC §2.5). With writes off, the default, the page's Extend
    button is greyed out and the server answers 403, and every phone was still told to open
    and extend, then signed out at the deadline (review of #243, round 3, 12/13). The line
    follows the switch as it is when the warning goes."""
    world.kit.runtime.set_auto_off(T0 + timedelta(minutes=5))
    world.later(30)
    assert [payload["body"] for _device, payload in world.pushes()] == [
        AUTO_OFF_READ_ONLY_BODY,
        AUTO_OFF_READ_ONLY_BODY,
    ]
    world.kit.runtime.set_allow_write(True)
    world.kit.runtime.set_auto_off(world.clock.now + timedelta(minutes=8))
    world.later(30)
    assert [payload["body"] for _device, payload in world.pushes()][2:] == [
        AUTO_OFF_BODY,
        AUTO_OFF_BODY,
    ]
    assert world.titles()[2:] == every_device(auto_off_title(8))


@pytest.mark.parametrize(
    ("left", "minutes"),
    [
        (timedelta(minutes=10), 10),
        (timedelta(minutes=9, seconds=31), 10),
        (timedelta(minutes=5), 5),
        (timedelta(seconds=20), 1),
    ],
)
def test_the_auto_off_warning_says_the_minutes_really_left(
    world: World, left: timedelta, minutes: int
) -> None:
    """``serve --auto-off 5`` is warned of at its first check, with five minutes, not ten."""
    world.kit.runtime.set_auto_off(T0 + timedelta(seconds=30) + left)
    world.later(30)
    assert world.titles() == every_device(auto_off_title(minutes))


def test_a_deadline_written_in_local_time_is_read_as_local_time(world: World) -> None:
    """The TUI hands the server a naive local ``datetime.now()`` (SPEC §2.5)."""
    world.kit.runtime.set_auto_off(
        (world.clock.now + timedelta(minutes=5)).astimezone().replace(tzinfo=None)
    )
    world.later(30)
    assert world.titles() == every_device(auto_off_title(5))


def test_nothing_is_pushed_past_the_auto_off_deadline(world: World) -> None:
    """From the deadline on every request is a 404 and every socket closes, but the TUI turns
    Remote off at its next 30 s check, and only then is the farewell sent: a question that
    became pushable in between went out, "coder-auth needs you", its link a 404, after the
    page said Remote is off and before the farewell that promised no more. What was gathered
    is let go unmarked, so a deadline moved later pushes it then."""
    world.kit.runtime.set_auto_off(T0 + timedelta(seconds=6))
    item = needs_item(1)
    world.scan(item)
    world.later(3)
    world.scan(item)  # pushable: its window closes at T0 + 8 s, past the deadline
    world.later(5)
    world.later(30)
    assert world.transport.sent == []
    assert item.id not in load_push_state().pushed
    world.kit.runtime.set_auto_off(world.clock.now + timedelta(hours=1))
    world.scan(item)
    world.later(5)
    assert world.titles() == every_device("aisquare-cli: coder-auth needs you")


def test_what_a_throttle_held_is_not_pushed_past_the_auto_off_deadline(world: World) -> None:
    first, second = needs_item(1), needs_item(2)
    world.scan(first)
    world.scan(first)
    world.later(5)  # T0 + 5 s: pushed, each device's throttle running to T0 + 25 s
    world.kit.runtime.set_auto_off(T0 + timedelta(seconds=15))
    world.scan(first, second)
    world.later(3)
    world.scan(first, second)
    world.later(5)  # T0 + 13 s: the second is owed to both devices, held by the throttle
    world.later(20)  # T0 + 33 s: the throttle is over, and so is Remote
    assert world.titles() == every_device("aisquare-cli: coder-auth needs you")
    assert second.id not in load_push_state().pushed


def test_no_warning_goes_out_past_the_auto_off_deadline(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [{"id": DEVICES[0], "expires_at": (T0 + timedelta(hours=23)).isoformat()}]
    monkeypatch.setattr(world.kit.runtime, "device_rows", lambda: rows)
    world.kit.runtime.set_auto_off(T0 + timedelta(seconds=10))
    world.later(30)
    assert world.transport.sent == [], "the expiry warning of a Remote that is off"


def test_a_system_push_skips_the_throttle(world: World) -> None:
    item = needs_item(1)
    world.later(19)  # the system check is due at T0 + 30 s
    world.scan(item)
    world.later(3)
    world.scan(item)  # the window closes at T0 + 27 s
    world.kit.runtime.set_auto_off(T0 + timedelta(minutes=8))
    world.later(5)
    assert world.titles() == every_device("aisquare-cli: coder-auth needs you")
    world.later(3)  # T0 + 30 s: 3 s after the needs push
    assert world.titles()[2:] == every_device(auto_off_title(8))


def test_the_expiry_warning_goes_only_to_the_expiring_device(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [
        {"id": DEVICES[0], "expires_at": (T0 + timedelta(hours=23)).isoformat()},
        {"id": DEVICES[1], "expires_at": (T0 + timedelta(days=3)).isoformat()},
    ]
    monkeypatch.setattr(world.kit.runtime, "device_rows", lambda: rows)
    world.later(30)
    assert world.titles() == [(DEVICES[0], EXPIRY_TITLE)]
    world.later(30)
    assert len(world.transport.sent) == 1, "once per expiry"


@contextlib.contextmanager
def _process_zone(monkeypatch: pytest.MonkeyPatch, zone: str) -> Iterator[ZoneInfo]:
    """The process's local time is ``zone``'s inside, put back after; skipped where ``time``
    has no ``tzset`` (Windows), as ``test_reset_formatter.py``'s clock is."""
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset is POSIX-only: the process zone cannot be switched for the test")
    with monkeypatch.context() as local:
        local.setenv("TZ", zone)
        time.tzset()
        try:
            yield ZoneInfo(zone)
        finally:
            local.undo()
            time.tzset()


ZONES = pytest.mark.parametrize(
    "zone", ["Pacific/Kiritimati", "Etc/GMT+12"], ids=["utc+14", "utc-12"]
)


@ZONES
def test_a_device_stamp_without_an_offset_is_read_as_utc_as_the_server_reads_it(
    world: World, monkeypatch: pytest.MonkeyPatch, zone: str
) -> None:
    """The stamps ``remote.json`` holds carry their offset. A device's without one, a hand
    edit, is UTC to the server, which prunes the device by it (``_remote_instant``); the
    sender read it as the machine's local time, the rule for a naive ``auto_off_at`` alone.
    Fourteen hours ahead of UTC, the phone with 30 hours left was told its sign-in ends in
    24 h; twelve behind, the one with 23 hours left was never told."""

    def naive_utc(left: timedelta) -> str:
        return (T0 + left).astimezone(UTC).replace(tzinfo=None).isoformat()

    rows = [
        {"id": DEVICES[0], "expires_at": naive_utc(timedelta(hours=30))},
        {"id": DEVICES[1], "expires_at": naive_utc(timedelta(hours=23))},
    ]
    monkeypatch.setattr(world.kit.runtime, "device_rows", lambda: rows)
    with _process_zone(monkeypatch, zone):
        world.later(30)
    assert world.titles() == [(DEVICES[1], EXPIRY_TITLE)]


@ZONES
def test_an_auto_off_written_without_an_offset_is_still_read_as_local_time(
    world: World, monkeypatch: pytest.MonkeyPatch, zone: str
) -> None:
    """An ``auto_off_at`` an earlier build wrote as the TUI's naive local time (SPEC §2.5),
    which the server reads so, in a zone where local time is not UTC: five minutes left is
    warned of as five minutes."""
    with _process_zone(monkeypatch, zone) as local:
        wall = (T0 + timedelta(seconds=30, minutes=5)).astimezone(local).replace(tzinfo=None)
        remote = {"allow_write": True, "auto_off_at": wall.isoformat()}
        monkeypatch.setattr(world.kit.runtime, "remote_json", lambda: remote)
        world.later(30)
    assert world.titles() == every_device(auto_off_title(5))


def test_the_farewell_reaches_devices_revoked_right_after_it(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Their subscriptions are read before ``push_farewell`` returns. The sending thread is
    held until the revoke has pruned them, so a farewell that read them any later finds
    none, every time, rather than now and then."""
    released = threading.Event()
    real_load = remote_push.load_push_state

    def held_in_the_sender() -> remote_push.PushState:
        if threading.current_thread().name == "asq-remote-push-now":
            assert released.wait(10)
        return real_load()

    monkeypatch.setattr(remote_push, "load_push_state", held_in_the_sender)
    transport = Transport()
    push_farewell(list(DEVICES), "remote off", transport=transport)
    push_unsubscribe_device("dev_other", set())  # the revoke's next write: both pruned
    assert real_load().subscriptions == {}
    released.set()
    transport.wait_for(2)
    world.transport.sent = transport.sent
    assert sorted(world.titles()) == every_device(FAREWELL_TITLE)
    assert {payload["url"] for _device, payload in world.pushes()} == {None}


def test_the_farewell_never_waits_on_a_push_service(world: World) -> None:
    released = threading.Event()

    def stuck(endpoint: str, headers: dict[str, str], body: bytes) -> int:
        released.wait(10)
        return 201

    started = time.monotonic()
    push_farewell(list(DEVICES), "auto-off", transport=stuck)
    assert time.monotonic() - started < 1.0
    released.set()


def test_a_farewell_queued_as_the_process_exits_still_arrives(world: World, tmp_path: Path) -> None:
    """``asq remote serve`` returns a quarter of a second after its auto-off queued the
    farewell, and the TUI can quit right after Remote was turned off: the daemon thread
    sending it died with the process, inside the TLS handshake. The process now waits for
    it on its way out. A real interpreter exits here, so a real exit is what is tested."""
    delivered = tmp_path / "delivered.txt"
    script = tmp_path / "farewell_then_exit.py"
    script.write_text(
        "import sys, time\n"
        "from aisquare.services import remote_push\n"
        "def slow_push_service(endpoint, headers, body):\n"
        "    time.sleep(0.5)\n"
        "    with open(sys.argv[1], 'a', encoding='utf-8') as out:\n"
        "        out.write(endpoint + '\\n')\n"
        "    return 201\n"
        "remote_push.push_farewell(sys.argv[2:], 'auto-off', transport=slow_push_service)\n",
        encoding="utf-8",
    )
    exited = subprocess.run(
        [sys.executable, str(script), str(delivered), *DEVICES],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert exited.returncode == 0, exited.stderr
    assert delivered.exists(), "the process exited without waiting for its farewell"
    assert sorted(delivered.read_text(encoding="utf-8").split()) == sorted(
        browser.endpoint for browser in world.browsers.values()
    )


def test_the_wait_at_exit_ends_at_its_deadline(world: World) -> None:
    """A push service that never answers costs an exiting process a bounded wait, and the
    wait says whether a push was still sending when it gave up."""
    released = threading.Event()

    def stuck(endpoint: str, headers: dict[str, str], body: bytes) -> int:
        released.wait(10)
        return 201

    push_farewell(list(DEVICES), "auto-off", transport=stuck)
    started = time.monotonic()
    assert push_drain(0.2) is False
    assert time.monotonic() - started < 2.0
    released.set()
    assert push_drain(10) is True


def test_the_lockout_alert_goes_once_per_trip(world: World) -> None:
    transport = Transport()
    text = "New unlocks are paused for 30 min."
    push_security_alert(list(DEVICES), text, transport=transport, now=T0)
    transport.wait_for(2)
    push_security_alert(list(DEVICES), text, transport=transport, now=T0 + timedelta(minutes=5))
    push_security_alert(list(DEVICES), text, transport=transport, now=T0 + timedelta(minutes=31))
    transport.wait_for(4)
    time.sleep(0.2)
    world.transport.sent = transport.sent
    assert len(world.pushes()) == 4, "the second call is the same trip"
    assert {payload["title"] for _device, payload in world.pushes()} == {LOCKOUT_TITLE}
    assert {payload["body"] for _device, payload in world.pushes()} == {text}


def test_one_shot_pushes_without_a_subscription_send_nothing(world: World) -> None:
    transport = Transport()
    push_farewell(["dev_unknown"], "remote off", transport=transport)
    push_security_alert(["dev_unknown"], "x", transport=transport, now=T0)
    time.sleep(0.2)
    assert transport.sent == []


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


def test_only_a_change_of_subscription_is_audited(
    app: Any, runtime: Runtime, roster: set[str]
) -> None:
    """Both routes need no write switch, and nothing trims the audit log: a read-only device
    sending its subscription again, or unsubscribing with none, wrote a line each time, about
    96 a second in a loop (review of #243, sweep of round 3). The answers are as before."""
    client, _device = unlocked(app, runtime, roster)
    assert runtime.allow_write is False
    subscribe = f"{base(runtime)}/api/push/subscribe"
    subscription = f"{base(runtime)}/api/push/subscription"
    nothing = client.delete(subscription)
    assert nothing.status_code == 200 and nothing.json() == {"subscribed": False}
    phone = Browser(f"{FCM}phone")
    for _ in range(3):
        sent = client.post(subscribe, json=phone.subscription())
        assert sent.status_code == 201 and sent.json() == {"subscribed": True}
    assert client.delete(subscription).status_code == 200
    assert client.delete(subscription).status_code == 200
    assert audited("push/subscribe") == ["fcm.googleapis.com"]
    assert audited("push/subscription") == ["-"]


def test_turning_notifications_on_and_off_in_a_loop_is_paced(
    app: Any, runtime: Runtime, roster: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every on and every off IS a change, so a loop of them still wrote two lines a round.
    Six a minute per device, subscribes and unsubscribes together, as a test push is paced;
    past that a 429 says how long to wait, and nothing is stored or audited. Another device
    has six of its own."""
    client, device = unlocked(app, runtime, roster)
    other, _other_device = unlocked(app, runtime, roster)
    phone = Browser(f"{FCM}phone")
    subscribe = f"{base(runtime)}/api/push/subscribe"
    subscription = f"{base(runtime)}/api/push/subscription"
    answers = []
    for _ in range(remote_push.PUSH_SUBSCRIPTION_CALLS // 2):
        answers.append(client.post(subscribe, json=phone.subscription()).status_code)
        answers.append(client.delete(subscription).status_code)
    assert answers == [201, 200] * (remote_push.PUSH_SUBSCRIPTION_CALLS // 2)
    refused = client.post(subscribe, json=phone.subscription())
    assert (refused.status_code, refused.json()["error"]) == (429, "push_subscription_throttled")
    assert 1 <= int(refused.headers["retry-after"]) <= 60
    assert client.delete(subscription).status_code == 429
    assert device not in load_push_state().subscriptions
    assert len(audited("push/subscribe")) == len(audited("push/subscription")) == 3
    theirs = Browser(f"{FCM}theirs")
    assert other.post(subscribe, json=theirs.subscription()).status_code == 201
    monkeypatch.setattr(remote_push, "PUSH_SUBSCRIPTION_WINDOW_SECONDS", 0.0)
    assert client.post(subscribe, json=phone.subscription()).status_code == 201


def test_the_subscription_routes_ask_for_the_devices_off_the_event_loop(
    app: Any, runtime: Runtime, roster: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``push_device_ids`` is ``Runtime.device_ids``, which reads ``remote.json`` again and
    hashes it: file work, which these routes keep off the event loop that serves every
    request and socket. Both asked for it there, before their worker thread."""
    client, _device = unlocked(app, runtime, roster)
    on_the_loop: list[bool] = []

    def device_ids(kit: object) -> frozenset[str]:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            on_the_loop.append(False)
        else:
            on_the_loop.append(True)
        return frozenset(roster)

    monkeypatch.setattr(remote_push, "push_device_ids", device_ids)
    phone = Browser(f"{FCM}phone")
    subscribed = client.post(f"{base(runtime)}/api/push/subscribe", json=phone.subscription())
    assert subscribed.status_code == 201
    assert client.delete(f"{base(runtime)}/api/push/subscription").status_code == 200
    assert on_the_loop == [False, False]


def test_the_push_routes_write_their_audit_lines_off_the_event_loop(
    app: Any, runtime: Runtime, roster: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An audit line opens ``remote-audit.log`` and appends to it, and the first one makes the
    file and restricts it to this account, on Windows an ``icacls`` run: file work, which the
    server's own routes do in a worker thread. These three did it on the event loop."""
    transport = Transport()
    monkeypatch.setattr(remote_push, "push_https_transport", transport)
    client, _device = unlocked(app, runtime, roster)
    written: list[tuple[str, bool]] = []
    audit = runtime.audit

    def audit_where_it_runs(device_id: str, endpoint: str, summary: str) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            written.append((endpoint, False))
        else:
            written.append((endpoint, True))
        audit(device_id, endpoint, summary)

    monkeypatch.setattr(runtime, "audit", audit_where_it_runs)
    phone = Browser(f"{FCM}phone")
    subscribed = client.post(f"{base(runtime)}/api/push/subscribe", json=phone.subscription())
    assert subscribed.status_code == 201
    assert client.post(f"{base(runtime)}/api/push/test").status_code == 202
    transport.wait_for(1)
    assert client.delete(f"{base(runtime)}/api/push/subscription").status_code == 200
    assert written == [
        ("push/subscribe", False),
        ("push/test", False),
        ("push/subscription", False),
    ]
    assert audited("push/subscribe") == ["fcm.googleapis.com"]
    assert audited("push/test") == audited("push/subscription") == ["-"]


def test_the_pace_counts_within_its_window_and_forgets_a_device_once_it_is_quiet() -> None:
    clock = [0.0]
    pace = remote_server._RateLimiter(lambda: clock[0])
    assert [pace.limiter_wait_seconds("dev_a", 2, 60.0) for _ in range(3)] == [None, None, 60]
    clock[0] = 59.5
    assert pace.limiter_wait_seconds("dev_a", 2, 60.0) == 1, "the first call ages out in 0.5 s"
    assert pace.limiter_wait_seconds("dev_b", 2, 60.0) is None, "every device has its own"
    clock[0] = 60.0
    assert pace.limiter_wait_seconds("dev_a", 2, 60.0) is None
    clock[0] = 200.0
    assert pace.limiter_wait_seconds("dev_c", 2, 60.0) is None
    assert list(pace._attempts) == ["dev_c"], "the quiet devices are forgotten"


def test_the_push_routes_pace_with_the_servers_one_rate_limiter(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The push routes paced their calls with a copy of the unlock limiter: the same sliding
    window in two places, where a fix to one missed the other (review of #243, round 4)."""
    made: list[object] = []

    class Counted(remote_server._RateLimiter):
        def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
            super().__init__(clock)
            made.append(self)

    monkeypatch.setattr(remote_server, "_RateLimiter", Counted)
    remote_push.push_routes(RemoteKit(runtime))
    assert len(made) == 2, "the test push's pace, and the subscriptions' pace"


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


def test_the_lifespan_starts_the_sender_on_the_watcher_and_stops_it(
    app: Any, runtime: Runtime
) -> None:
    with make_client(app):
        sender = app.kit.lane_state["push"]
        assert isinstance(sender, RemotePushSender)
        assert sender.enqueue_needs_push in app.kit.needs_listeners
        threads = [t for t in threading.enumerate() if t.name == "asq-remote-push"]
        assert threads, "the sender's thread runs"
    assert "push" not in app.kit.lane_state
    assert app.kit.needs_listeners == []
    for thread in threads:
        thread.join(3)
        assert not thread.is_alive()


def test_two_scans_through_the_listener_push_each_new_id_once(
    runtime: Runtime, roster: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real thread, on the listener contract (``(all items, scanned_at)`` after every
    scan); then a restart with ``pushed`` on file, which pushes none of it again."""
    monkeypatch.setattr(remote_push, "PUSH_COALESCE_SECONDS", 0.05)
    transport = Transport()
    monkeypatch.setattr(remote_push, "push_https_transport", transport)
    roster.add("dev_aaaaaaaa")
    phone = Browser(f"{FCM}phone")
    push_subscribe_device("dev_aaaaaaaa", phone.record(), roster)
    load_or_create_vapid_keys()
    for attempt in range(2):
        kit = RemoteKit(runtime)
        watcher = Watcher()
        kit.lane_state["needs"] = watcher
        stop = start_push_sender(kit)
        assert stop is not None
        try:
            due = datetime.now(UTC) - timedelta(minutes=1)
            items = [needs_item(1, push_after=due), needs_item(2, agent="coder-db", push_after=due)]
            watcher.items = items
            for listener in list(kit.needs_listeners):
                listener(items, datetime.now(UTC))
                listener(items, datetime.now(UTC))
            if attempt == 0:
                transport.wait_for(1)  # however slow the machine: the push itself is waited on
            time.sleep(0.3)  # long enough for a second push, or a restart's first, to show
        finally:
            stop()
        assert len(transport.sent) == 1, f"run {attempt + 1}: one push, then none on restart"
    assert phone.read(transport.sent[0][2])["ids"] == [items[0].id, items[1].id]


# --- with the real device model (lane b-security) ---------------------------------------------


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


def test_with_the_real_device_rows_the_expiring_phone_is_warned(app: Any, runtime: Runtime) -> None:
    client = make_client(app)
    assert unlock(client, runtime).status_code == 200
    (row,) = runtime.device_rows()
    phone = Browser(f"{FCM}phone")
    assert opt_in(client, runtime, phone) == 201
    expires = datetime.fromisoformat(str(row["expires_at"]))
    transport = Transport()
    clock = Clock(expires - timedelta(hours=23))
    RemotePushSender(app.kit, transport=transport, clock=clock).push_run_due()
    ((_endpoint, _headers, body),) = transport.sent
    assert phone.read(body)["title"] == EXPIRY_TITLE


# --- the TUI: the static domain and the tunnel watchdog (SPEC §5.8, §5.10 item 12) ------------


class StubTunnel(NgrokTunnel):
    """A tunnel that comes up at once with ``url`` (or never announces one), and dies when
    the test says so. ``exit_error``: it starts, then exits at once with that error before
    it announces anything, as ngrok does when its static domain is still held elsewhere.
    Its own fake, so ``test_remote_control.py`` stays its lanes'."""

    def __init__(
        self,
        port: int,
        url: str | None,
        failure: str | None = None,
        exit_error: str | None = None,
    ) -> None:
        super().__init__(port, which=lambda _name: None)
        self._stub_url = url
        self._stub_failure = failure
        self._stub_exit_error = exit_error
        self.alive = False
        self.stopped = False

    def start_tunnel(self) -> str | None:
        if self._stub_failure is not None:
            self.error = self._stub_failure
            return self._stub_failure
        if self._stub_exit_error is not None:  # what the log reader leaves of such an exit
            self.error = self._stub_exit_error
            self._url_ready.set()
            return None
        self.alive = True
        self.public_url = self._stub_url
        if self._stub_url is not None:
            self._url_ready.set()
        return None

    @property
    def running(self) -> bool:
        return self.alive

    def stop_tunnel(self) -> None:
        self.stopped = True
        self.alive = False


class TunnelShop:
    """Hands out one :class:`StubTunnel` per start, with the next of ``urls`` (and of
    ``failures`` and ``exits``)."""

    def __init__(
        self,
        *urls: str | None,
        failures: tuple[str | None, ...] = (),
        exits: tuple[str | None, ...] = (),
    ) -> None:
        self.urls = list(urls)
        self.failures = list(failures)
        self.exits = list(exits)
        self.made: list[StubTunnel] = []

    def __call__(self, port: int) -> NgrokTunnel:
        url = self.urls.pop(0) if self.urls else None
        failure = self.failures.pop(0) if self.failures else None
        exit_error = self.exits.pop(0) if self.exits else None
        tunnel = StubTunnel(port, url, failure, exit_error)
        self.made.append(tunnel)
        return tunnel


class LocalClock:
    """The controller's clock: naive local time, as ``datetime.now`` gives it."""

    def __init__(self) -> None:
        self.now = datetime(2026, 10, 7, 10, 0)

    def __call__(self) -> datetime:
        return self.now


def controller_with(shop: TunnelShop, clock: LocalClock) -> tuple[RemoteController, FakeServer]:
    server = FakeServer()
    controller = RemoteController(server=server, tunnel_factory=shop, now=clock, url_timeout=0.2)
    controller.turn_on()
    waited(controller)
    return controller, server


def waited(controller: RemoteController) -> None:
    """Join the thread that waits for ngrok's URL, so what it sets is there to assert."""
    assert controller._waiter is not None
    controller._waiter.join(5)


def test_the_tui_tells_the_server_where_phones_reach_it(isolated_home: Path) -> None:
    controller, server = controller_with(TunnelShop("https://first.ngrok-free.app"), LocalClock())
    link = f"https://first.ngrok-free.app/r/{server.token}/"
    assert controller.link_url() == link
    assert server.public_urls == [link]


def test_a_dead_tunnel_is_started_again_at_most_once_a_minute(isolated_home: Path) -> None:
    shop, clock = TunnelShop("https://first.ngrok-free.app", "https://second.ngrok-free.app",
                             "https://third.ngrok-free.app"), LocalClock()  # fmt: skip
    controller, server = controller_with(shop, clock)
    assert controller.revive_tunnel_if_dead() is False, "alive: nothing to do"
    shop.made[0].alive = False
    assert controller.revive_tunnel_if_dead() is True
    waited(controller)
    second = f"https://second.ngrok-free.app/r/{server.token}/"
    assert controller.tunnel is shop.made[1] and shop.made[0].stopped
    assert controller.link_url() == second and server.public_urls[-1] == second
    assert controller.message == "ngrok stopped — restarted it; the link changed"
    shop.made[1].alive = False
    clock.now += timedelta(seconds=59)
    assert controller.revive_tunnel_if_dead() is False, "a minute has not passed"
    assert len(shop.made) == 2
    clock.now += timedelta(seconds=1)
    assert controller.revive_tunnel_if_dead() is True
    assert len(shop.made) == 3


def test_a_static_domain_comes_back_on_the_same_link(isolated_home: Path) -> None:
    same = "https://remote-anmol.ngrok-free.app"
    shop, clock = TunnelShop(same, same), LocalClock()
    controller, server = controller_with(shop, clock)
    shop.made[0].alive = False
    assert controller.revive_tunnel_if_dead() is True
    waited(controller)
    assert controller.message == "ngrok stopped — restarted it"
    assert controller.link_url() == f"{same}/r/{server.token}/"


def test_a_tunnel_that_never_came_up_is_left_to_its_error(isolated_home: Path) -> None:
    """It failed for a reason the status line already says; a restart would fail the same."""
    shop, clock = TunnelShop(None), LocalClock()
    controller, _server = controller_with(shop, clock)
    shop.made[0].alive = False
    assert controller.revive_tunnel_if_dead() is False
    assert len(shop.made) == 1


def test_a_restart_that_fails_is_tried_again_the_next_minute(isolated_home: Path) -> None:
    shop = TunnelShop("https://first.ngrok-free.app", None, "https://third.ngrok-free.app",
                      failures=(None, "could not start ngrok: gone"))  # fmt: skip
    clock = LocalClock()
    controller, _server = controller_with(shop, clock)
    shop.made[0].alive = False
    assert controller.revive_tunnel_if_dead() is False
    assert controller.message == "could not start ngrok: gone"
    assert controller.tunnel is shop.made[0], "the dead one stays, to be revived later"
    clock.now += timedelta(seconds=60)
    assert controller.revive_tunnel_if_dead() is True


def test_a_restart_that_exits_before_it_announces_is_restarted_a_minute_later(
    isolated_home: Path,
) -> None:
    """The static domain is still held by the session that just died, or the network is
    down for a while: the restart exits before it announces a URL. The watchdog stopped
    for good there, and left the night's phone on a dead link. It keeps trying, once a
    minute, and says nothing changed once the same link is back."""
    same = "https://remote-anmol.ngrok-free.app"
    held = "failed to start tunnel: The endpoint is already online. ERR_NGROK_334"
    shop, clock = TunnelShop(same, None, same, exits=(None, held)), LocalClock()
    controller, server = controller_with(shop, clock)
    shop.made[0].alive = False
    assert controller.revive_tunnel_if_dead() is True
    waited(controller)
    assert controller.message == held
    assert controller.info is not None and controller.link_url() == controller.info.url_local
    clock.now += timedelta(seconds=59)
    assert controller.revive_tunnel_if_dead() is False, "a minute has not passed"
    clock.now += timedelta(seconds=1)
    assert controller.revive_tunnel_if_dead() is True
    waited(controller)
    assert len(shop.made) == 3 and shop.made[1].stopped
    assert controller.link_url() == f"{same}/r/{server.token}/"
    assert server.public_urls[-1] == f"{same}/r/{server.token}/"
    assert controller.message == "ngrok stopped — restarted it"


def test_a_new_remote_whose_first_tunnel_never_came_up_is_left_alone(
    isolated_home: Path,
) -> None:
    """Restarts in an earlier Remote do not make this one's first tunnel a restart: it never
    came up, for a reason a restart would hit again."""
    shop = TunnelShop("https://first.ngrok-free.app", "https://second.ngrok-free.app", None)
    clock = LocalClock()
    controller, _server = controller_with(shop, clock)
    shop.made[0].alive = False
    assert controller.revive_tunnel_if_dead() is True
    waited(controller)
    controller.turn_off()
    controller.turn_on()
    waited(controller)
    shop.made[2].alive = False
    clock.now += timedelta(minutes=5)
    assert controller.revive_tunnel_if_dead() is False
    assert len(shop.made) == 3


def test_remote_off_revives_nothing(isolated_home: Path) -> None:
    shop, clock = (
        TunnelShop("https://first.ngrok-free.app", "https://2.ngrok-free.app"),
        LocalClock(),
    )
    controller, _server = controller_with(shop, clock)
    shop.made[0].alive = False
    controller.turn_off()
    assert controller.revive_tunnel_if_dead() is False
    assert len(shop.made) == 1


def test_a_link_that_is_no_origin_is_not_noted_and_does_not_end_the_waiter(
    isolated_home: Path,
) -> None:
    """The server refuses a URL that is no public origin (a port, an IP). The thread that
    waited for it must carry on: after a revive, it is what says ngrok was restarted."""

    class Strict(FakeServer):
        def note_public_url(self, url: str | None) -> None:
            raise ValueError(f"{url!r} is on port 4443, not 443")

    server = Strict()
    shop, clock = TunnelShop("https://x.example:4443", "https://y.example:4443"), LocalClock()
    controller = RemoteController(server=server, tunnel_factory=shop, now=clock, url_timeout=0.2)
    controller.turn_on()
    waited(controller)
    assert controller.link_url() == f"https://x.example:4443/r/{server.token}/"
    shop.made[0].alive = False
    assert controller.revive_tunnel_if_dead() is True
    waited(controller)
    assert controller.message == "ngrok stopped — restarted it; the link changed"


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
        ["ngrok", "http", "8750", "--log=stdout", "--log-format=json", "--log-level=info",
         "--inspect=false", "--url=remote-anmol.ngrok-free.app"]
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
