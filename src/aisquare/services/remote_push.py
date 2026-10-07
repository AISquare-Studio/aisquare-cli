"""Web Push: the phone is pinged when something needs the human, page closed or not (SPEC §5).

The decision is split the way the lanes are. Needs-you decides WHETHER and WHEN
an item may be pushed (``NeedsItem.push_after``; ``None`` keeps it in the feed
only). This module decides HOW, and is strict about it: an item is pushed once,
only after two consecutive scans saw it, only if it is still there when its
5-second coalescing window closes, at most one notification per device every
20 s, and the payload holds fixed sentences and bounded names only. Never an
excerpt, a detail or anything else an agent wrote: a lock screen shows it to
whoever holds the phone.

**Keys and subscriptions** live in ``~/.aisquare/remote-push.json``, owner-only
like ``remote.json``: one VAPID key pair (P-256, made on first use and never
rotated, because every subscription a browser holds was made against its
public half), one subscription per device id, and the ids already pushed, so a
restart pushes nothing twice. Subscriptions belong to DEVICES, not to cookies:
a device signed out for being idle still gets its pushes (a quiet day must not
silence the phone), and a revoked, expired or regenerated-away device loses its
subscription at the next send or subscription write, with no hook into revoke.

**Who we talk to** is an exact allowlist of the four browsers' push services
(:func:`push_host_allowed`), so a subscription can never make this machine POST
to an address its sender chose.

**The crypto** is RFC 8291 (aes128gcm) and RFC 8292 (VAPID) on ``cryptography``,
imported lazily: without it ``GET api/push`` says how to install it, nothing
else changes, and ``remote_server._remote_dependency_error`` does not ask for it.

**Where a notification leads** is the one string a push must never learn from a
request: the server's public origin comes from the TUI's ngrok announcement,
``serve --public-url``, or ngrok's own local API, and the link is ``null``
without one (:meth:`RemoteKit.kit_public_url`).

Nothing here blocks the event loop. Routes do their file work in a worker
thread; every send runs on the sender's thread (``asq-remote-push``) or a
one-shot daemon thread, with a 10 s timeout, and a process on its way out waits
that long for its one-shot pushes (:func:`push_drain`), the farewell above all.
Lock order: the module's file lock is never held while calling into the
runtime, which takes its own.
"""

from __future__ import annotations

import atexit
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import math
import os
import queue
import re
import struct
import threading
import time
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from aisquare.core.atomic import write_replacing
from aisquare.core.paths import remote_push_path
from aisquare.services.remote_server import RequestError, check_public_origin

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric import ec
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.routing import BaseRoute

    from aisquare.services.remote_needs import NeedsItem
    from aisquare.services.remote_server import Device, RemoteKit

log = logging.getLogger(__name__)

PUSH_MIN_INTERVAL_SECONDS = 20.0
"""The shortest gap between two needs notifications to one device. System pushes skip it."""
PUSH_COALESCE_SECONDS = 5.0
"""How long the sender waits once an item becomes pushable, so a burst is one notification."""
AUTO_OFF_WARNING = timedelta(minutes=10)
"""How long before auto-off the phones are told, so one of them can extend it."""
EXPIRY_WARNING = timedelta(hours=24)
"""How long before a device's 7-day sign-in ends its phone is told its pushes end with it."""

PUSH_ENDPOINT_MAX = 1_024
"""The longest subscription endpoint accepted; the real services' are about 200 characters."""
PUSH_PLAINTEXT_MAX = 3_000
"""The most bytes of JSON one push carries, cut before encryption (:func:`push_plaintext`)."""
PUSH_RECORD_SIZE = 4_096
"""The aes128gcm record size the body declares; one record always holds the whole payload."""
PUSH_IDS_MAX = 50
"""The most item ids one notification lists. More is a fleet on fire, and the page refetches."""
PUSH_REASON_MAX = 160
"""A reason's length on a lock screen: needs-you's templates with two 40-character names fit."""
PUSH_TTL_SECONDS = 3_600
"""How long a push service keeps a push for a phone that is offline; older news is no news."""
PUSH_TIMEOUT_SECONDS = 10.0
PUSH_RESPONSE_MAX = 4_096
"""The most of a push service's answer that is read; only its status matters."""
PUSH_FAILURES_MAX = 3
"""Refusals in a row (400/401/403) after which a subscription is dropped as broken."""
PUSH_TEST_INTERVAL_SECONDS = 10.0
"""One test push per device this often: a test is a tap, never a loop."""
PUSH_SYSTEM_CHECK_SECONDS = 30.0
"""How often the sender looks at the auto-off deadline and the devices' expiry."""
PUSH_DISCOVERY_SECONDS = 60.0
"""How often, at most, the sender asks ngrok's local API for a public URL it was not told,
or asks again about the one the API named last time."""
PUSH_STOP_SECONDS = 2.0
"""How long stopping the server waits for a send in flight."""
PUSH_DRAIN_SECONDS = PUSH_TIMEOUT_SECONDS
"""How long a process on its way out waits for its one-shot pushes (:func:`push_drain`): a
push's own timeout, so a farewell that can arrive does, and one that cannot costs no more."""
PUSHED_KEEP = timedelta(days=7)
PUSHED_MAX = 1_000
"""``pushed`` keeps a week, and at most this many ids, the newest."""
LOCKOUT_ALERT_WINDOW = timedelta(minutes=30)
"""One lockout alert per window: the unlock budget's own 30 minutes."""

VAPID_SUBJECT = "https://github.com/AISquare-Studio/aisquare-cli"
"""Who sends (RFC 8292 ``sub``): where a push service turns when a sender misbehaves."""
VAPID_LIFETIME = timedelta(hours=12)
"""A VAPID JWT's ``exp``; the services refuse anything over 24 h."""
VAPID_REUSE = timedelta(hours=1)
"""How long one signed JWT serves its push service, so a burst signs once."""

PUSH_HOSTS = frozenset(
    {"fcm.googleapis.com", "updates.push.services.mozilla.com", "web.push.apple.com"}
)
"""Chrome's (and Android Edge's), Firefox's and Safari's push services, matched exactly."""
PUSH_HOST_SUFFIXES = (".push.apple.com", ".notify.windows.com")
"""Apple's and Windows' services, which shard by subdomain. The leading dot is the point:
``evilpush.apple.com`` ends in ``push.apple.com`` and is somebody else's."""

PUSH_INSTALL_HINT = "Web Push needs the cryptography package — pip install 'aisquare-cli[remote]'"

AUTO_OFF_TITLE = "Remote turns off in {minutes} min"
"""With the minutes left, rounded up: 10 when the first check inside :data:`AUTO_OFF_WARNING`
sees the deadline, fewer for one that was nearer from the start (``serve --auto-off 5``)."""
AUTO_OFF_BODY = "Open to extend it by an hour."
FAREWELL_TITLE = "Remote is off on the machine"
FAREWELL_BODY = "No more notifications until it is turned on again."
LOCKOUT_TITLE = "Someone is guessing the Remote password"
EXPIRY_TITLE = "Notifications on this phone end in 24 h"
EXPIRY_BODY = "Its 7-day sign-in ends then; unlock again afterwards to keep them."
TEST_TITLE = "Notifications work"
TEST_BODY = "This is the test you asked for from aisquare remote."

PushTransport = Callable[[str, dict[str, str], bytes], int]
"""Endpoint, headers, encrypted body → the push service's HTTP status. Raises on a network
error. The tests' seam: nothing in them ever reaches a real push service."""
PushMessage = dict[str, object]
"""What a notification says, before encryption: ``{v, title, body, tag, url, ids}``."""

_B64URL = re.compile(r"[A-Za-z0-9_-]*\Z")
_DNS_NAME = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+\Z"
)
_ROUTE_ID = re.compile(r"ny_[0-9a-f]{16}\Z")
_ROUTE_SEGMENT = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")
"""What the page's router accepts in a card link (SPEC §6.6); anything else is left out."""


# --- small pieces -----------------------------------------------------------------------------


def _push_utc_now() -> datetime:
    return datetime.now(UTC)


def _push_iso(at: datetime) -> str:
    return at.astimezone(UTC).isoformat(timespec="seconds")


def _push_parse_time(raw: str) -> datetime | None:
    """An ISO time as an aware UTC datetime; a naive one is local time, as the TUI wrote it."""
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed.astimezone(UTC)


def _push_b64(data: bytes) -> str:
    """base64url without padding: what browsers send and the services expect."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _push_unb64(text: object) -> bytes | None:
    """The bytes of a base64url string (padding optional), or ``None`` for anything else.

    Strict about the alphabet: the stdlib decoder silently skips characters it does
    not know, which could turn garbage into a key of the right length.
    """
    if not isinstance(text, str):
        return None
    bare = text.rstrip("=")
    if not _B64URL.match(bare) or len(bare) % 4 == 1:
        return None
    return base64.urlsafe_b64decode(bare + "=" * (-len(bare) % 4))


def _push_json(payload: Mapping[str, object]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


_push_said: set[str] = set()


def _push_warn_once(key: str, message: str, *args: object) -> None:
    """A warning this process gives once: the sender reads and writes the file every 30 s."""
    if key not in _push_said:
        _push_said.add(key)
        log.warning(message, *args)


def push_crypto_missing() -> str | None:
    """Why Web Push cannot run here, or ``None`` when it can: it needs ``cryptography``.

    Found, not imported: ``GET api/push`` asks on every call, and a server that
    never pushes should not load the library for it.
    """
    import importlib.util

    return PUSH_INSTALL_HINT if importlib.util.find_spec("cryptography") is None else None


# --- which push services we talk to (SPEC §5.4) -----------------------------------------------


def push_host_allowed(endpoint: object) -> bool:
    """Whether ``endpoint`` is a push service this server may POST to.

    https on port 443, no user name, a DNS name (never an IP literal), and that
    name one of :data:`PUSH_HOSTS` exactly or under one of
    :data:`PUSH_HOST_SUFFIXES`. Anything else would let whoever subscribes choose
    where this machine sends requests, from inside the network it sits in.
    """
    import ipaddress
    from urllib.parse import urlsplit

    if not isinstance(endpoint, str) or not endpoint.isascii() or not endpoint.isprintable():
        return False
    if any(ch.isspace() for ch in endpoint):
        return False
    try:
        parts = urlsplit(endpoint)
        port = parts.port
    except ValueError:
        return False
    if parts.scheme != "https" or "@" in parts.netloc or port not in (None, 443):
        return False
    host = parts.hostname or ""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return False
    if not _DNS_NAME.match(host):
        return False
    return host in PUSH_HOSTS or host.endswith(PUSH_HOST_SUFFIXES)


# --- the crypto: RFC 8291 and RFC 8292 --------------------------------------------------------


def _push_public_bytes(private: ec.EllipticCurvePrivateKey) -> bytes:
    """The 65-byte uncompressed point ``0x04 || X || Y`` of ``private``'s public key."""
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    return private.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)


def _push_on_curve(point: bytes) -> bool:
    """Whether ``point`` is an uncompressed P-256 public key, on the curve."""
    from cryptography.hazmat.primitives.asymmetric import ec

    if len(point) != 65 or point[0] != 4:
        return False
    try:
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), point)
    except ValueError:
        return False
    return True


def encrypt_push_payload(
    plaintext: bytes,
    ua_public: bytes,
    auth_secret: bytes,
    *,
    salt: bytes | None = None,
    as_private: ec.EllipticCurvePrivateKey | None = None,
) -> bytes:
    """``plaintext`` encrypted to one browser (RFC 8291, over RFC 8188 ``aes128gcm``).

    The key is agreed between a fresh P-256 key of ours (``as_private``) and the
    browser's (``ua_public``), mixed with its ``auth_secret``: only that browser
    can read the result, and the push service in between learns its length. One
    record: ``salt(16) || rs(4096) || idlen(65) || as_public(65) || ciphertext``,
    the plaintext ending in the last-record delimiter ``0x02``. Only the tests
    fix ``salt`` and ``as_private`` (the RFC's own example).
    """
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if not _push_on_curve(ua_public):
        raise ValueError("the browser's key is not an uncompressed P-256 point")
    if len(auth_secret) != 16:
        raise ValueError("the browser's auth secret is not 16 bytes")
    if len(plaintext) > PUSH_RECORD_SIZE - 17:  # the 16-byte tag and the delimiter
        raise ValueError(f"{len(plaintext)} bytes do not fit one {PUSH_RECORD_SIZE}-byte record")
    salt = os.urandom(16) if salt is None else salt
    if len(salt) != 16:
        raise ValueError("the salt is not 16 bytes")
    if as_private is None:
        as_private = ec.generate_private_key(ec.SECP256R1())
    as_public = _push_public_bytes(as_private)
    browser = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    shared = as_private.exchange(ec.ECDH(), browser)
    prk_key = hmac.new(auth_secret, shared, hashlib.sha256).digest()
    key_info = b"WebPush: info\x00" + ua_public + as_public
    ikm = hmac.new(prk_key, key_info + b"\x01", hashlib.sha256).digest()
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    cek = hmac.new(prk, b"Content-Encoding: aes128gcm\x00\x01", hashlib.sha256).digest()[:16]
    nonce = hmac.new(prk, b"Content-Encoding: nonce\x00\x01", hashlib.sha256).digest()[:12]
    ciphertext = AESGCM(cek).encrypt(nonce, plaintext + b"\x02", None)
    return salt + struct.pack("!IB", PUSH_RECORD_SIZE, len(as_public)) + as_public + ciphertext


@dataclass(frozen=True)
class VapidKeys:
    """The server's one VAPID key pair (RFC 8292), as ``remote-push.json`` keeps it."""

    private_key: str
    """base64url of the raw 32-byte private scalar."""
    public_key: str
    """base64url of the 65-byte uncompressed point: the page's ``applicationServerKey``."""
    created_at: str


def _push_private_key(keys: VapidKeys) -> ec.EllipticCurvePrivateKey:
    from cryptography.hazmat.primitives.asymmetric import ec

    raw = _push_unb64(keys.private_key)
    if raw is None or len(raw) != 32:
        raise ValueError("the stored VAPID private key is not 32 bytes of base64url")
    return ec.derive_private_key(int.from_bytes(raw, "big"), ec.SECP256R1())


def _push_vapid_usable(keys: VapidKeys) -> bool:
    """Whether a stored pair is whole: a private scalar whose public half is the one stored."""
    try:
        private = _push_private_key(keys)
    except ValueError:
        return False
    return _push_b64(_push_public_bytes(private)) == keys.public_key


def _push_new_vapid_keys(now: datetime) -> VapidKeys:
    from cryptography.hazmat.primitives.asymmetric import ec

    private = ec.generate_private_key(ec.SECP256R1())
    scalar = private.private_numbers().private_value.to_bytes(32, "big")
    return VapidKeys(_push_b64(scalar), _push_b64(_push_public_bytes(private)), _push_iso(now))


_vapid_signed: dict[tuple[str, str], tuple[datetime, str]] = {}
"""``(public key, audience)`` → ``(when signed, JWT)``, reused for :data:`VAPID_REUSE`."""
_vapid_signed_lock = threading.Lock()


def vapid_authorization(endpoint: str, keys: VapidKeys, *, now: datetime) -> str:
    """The ``Authorization`` header of one push (RFC 8292): ``vapid t=<jwt>, k=<public key>``.

    The JWT is ES256 over ``{aud, exp, sub}``: ``aud`` is the push service's
    origin and ``exp`` is 12 h ahead. The signature is raw ``r || s`` (32 + 32
    bytes), not the DER ``cryptography`` returns. One JWT per push service is
    reused for an hour.
    """
    from urllib.parse import urlsplit

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    audience = f"https://{urlsplit(endpoint).hostname}"
    with _vapid_signed_lock:
        for key, (signed_at, _jwt) in list(_vapid_signed.items()):
            if not timedelta(0) <= now - signed_at < VAPID_REUSE:
                del _vapid_signed[key]
        cached = _vapid_signed.get((keys.public_key, audience))
    if cached is not None:
        return f"vapid t={cached[1]}, k={keys.public_key}"
    header = _push_b64(_push_json({"typ": "JWT", "alg": "ES256"}))
    exp = int((now + VAPID_LIFETIME).timestamp())
    claims = _push_b64(_push_json({"aud": audience, "exp": exp, "sub": VAPID_SUBJECT}))
    signing_input = f"{header}.{claims}".encode("ascii")
    der = _push_private_key(keys).sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    jwt = f"{header}.{claims}.{_push_b64(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"
    with _vapid_signed_lock:
        _vapid_signed[(keys.public_key, audience)] = (now, jwt)
    return f"vapid t={jwt}, k={keys.public_key}"


# --- remote-push.json (SPEC §5.2) -------------------------------------------------------------


@dataclass(frozen=True)
class PushSubscriptionRecord:
    """One device's subscription: where its push service listens, and the keys it reads with."""

    endpoint: str
    p256dh: str
    """base64url of the browser's 65-byte P-256 public key."""
    auth: str
    """base64url of the browser's 16-byte auth secret."""
    created_at: str
    failures: int = 0
    """Sends in a row that failed (SPEC §5.7); a success resets it."""


@dataclass
class PushState:
    """``remote-push.json`` in memory: the keys, each device's subscription, what was pushed."""

    vapid: VapidKeys | None = None
    subscriptions: dict[str, PushSubscriptionRecord] = field(default_factory=dict)
    """By device id: one each, and none for a device the runtime no longer has."""
    pushed: dict[str, str] = field(default_factory=dict)
    """An item id, or a ``sys:`` key for a system push, → when it was pushed."""

    def push_state_json(self) -> dict[str, object]:
        vapid = self.vapid
        return {
            "version": 1,
            "vapid": None
            if vapid is None
            else {
                "private_key": vapid.private_key,
                "public_key": vapid.public_key,
                "created_at": vapid.created_at,
            },
            "subscriptions": {
                device_id: {
                    "endpoint": record.endpoint,
                    "p256dh": record.p256dh,
                    "auth": record.auth,
                    "created_at": record.created_at,
                    "failures": record.failures,
                }
                for device_id, record in self.subscriptions.items()
            },
            "pushed": dict(self.pushed),
        }


def _push_state_from_json(raw: object) -> PushState:
    """What a parsed file holds that this version understands; the rest is dropped."""
    state = PushState()
    if not isinstance(raw, dict):
        return state
    vapid = raw.get("vapid")
    if isinstance(vapid, dict):
        private, public = vapid.get("private_key"), vapid.get("public_key")
        if isinstance(private, str) and isinstance(public, str):
            state.vapid = VapidKeys(private, public, str(vapid.get("created_at") or ""))
    subscriptions = raw.get("subscriptions")
    if isinstance(subscriptions, dict):
        for device_id, row in subscriptions.items():
            if not isinstance(row, dict):
                continue
            endpoint, p256dh, auth = row.get("endpoint"), row.get("p256dh"), row.get("auth")
            if not (
                isinstance(endpoint, str) and isinstance(p256dh, str) and isinstance(auth, str)
            ):
                continue
            failures = row.get("failures")
            state.subscriptions[str(device_id)] = PushSubscriptionRecord(
                endpoint=endpoint,
                p256dh=p256dh,
                auth=auth,
                created_at=str(row.get("created_at") or ""),
                failures=failures if type(failures) is int and failures >= 0 else 0,
            )
    pushed = raw.get("pushed")
    if isinstance(pushed, dict):
        state.pushed = {str(key): at for key, at in pushed.items() if isinstance(at, str)}
    return state


def load_push_state() -> PushState:
    """``remote-push.json`` as it is on disk; a missing or malformed file is an empty state.

    Malformed costs the keys, which the next ``GET api/push`` makes again, and so
    every subscription: what a deleted file costs, and nothing worse. A file that
    exists but cannot be read raises ``OSError`` instead: new keys over keys that
    are merely unreadable right now would orphan every phone for good.
    """
    path = remote_push_path()
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return PushState()
    try:
        raw = json.loads(data.decode("utf-8"))
    except (ValueError, RecursionError):
        _push_warn_once(
            "malformed",
            "remote: %s is not valid JSON; push keys and subscriptions start over",
            path,
        )
        return PushState()
    return _push_state_from_json(raw)


def save_push_state(state: PushState) -> None:
    """Write ``remote-push.json``, owner-only from the moment it has its name.

    It holds the VAPID private key and the subscriptions, which are how to reach
    this human's phone: the same care as ``remote.json``
    (``core.atomic.write_replacing(owner_only=True)``).
    """
    path = remote_push_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(state.push_state_json(), indent=2).encode("utf-8")
    if not write_replacing(path, encoded, owner_only=True):
        _push_warn_once(
            "unrestricted",
            "remote: could not restrict %s to your account — other users on this machine "
            "may be able to read the push key and the subscriptions in it",
            path,
        )


_push_file_lock = threading.Lock()
"""Serializes every read-modify-write of ``remote-push.json`` in this process. Never held
while calling into the runtime, which takes its own lock and may call in here."""


@contextlib.contextmanager
def _push_state_edit() -> Iterator[PushState]:
    """``remote-push.json`` to change in place: written back only when it did change."""
    with _push_file_lock:
        state = load_push_state()
        before = state.push_state_json()
        yield state
        if state.push_state_json() != before:
            save_push_state(state)


def _push_prune(state: PushState, live: Collection[str]) -> None:
    """Drop the subscriptions of devices the runtime no longer has (SPEC §5.3)."""
    for device_id in [d for d in state.subscriptions if d not in live]:
        del state.subscriptions[device_id]
        log.info("remote: dropped the push subscription of %s, a device that is gone", device_id)


def _push_prune_pushed(pushed: Mapping[str, str], now: datetime) -> dict[str, str]:
    """``pushed`` without what is a week old, and at most :data:`PUSHED_MAX` of the newest."""
    dated = [
        (at, key, stamp)
        for key, stamp in pushed.items()
        if (at := _push_parse_time(stamp)) is not None and now - at < PUSHED_KEEP
    ]
    dated.sort()
    return {key: stamp for _at, key, stamp in dated[-PUSHED_MAX:]}


def load_or_create_vapid_keys() -> VapidKeys:
    """The server's VAPID key pair, made on first use and kept for good.

    Never rotated: a push signed with another key is refused (401/403) until the
    phone subscribes again, so a new key silences every phone at once. A stored
    pair that is not whole is replaced, which costs exactly that.
    """
    with _push_state_edit() as state:
        if state.vapid is None or not _push_vapid_usable(state.vapid):
            state.vapid = _push_new_vapid_keys(_push_utc_now())
        return state.vapid


def push_device_ids(kit: RemoteKit) -> frozenset[str]:
    """Every device a push may still reach: signed in, or signed out for being idle (SPEC §5.3).

    Revoked, expired and regenerated-away devices are not here, and that is the
    whole of how their subscriptions end: every send and every subscription
    write drops the entries of devices missing from this set.
    """
    return frozenset(kit.runtime.device_ids())


def push_subscription_from_body(
    body: Mapping[str, Any], *, now: datetime
) -> PushSubscriptionRecord:
    """The subscription a ``PushSubscription.toJSON()`` body describes, or :class:`RequestError`.

    The endpoint: at most :data:`PUSH_ENDPOINT_MAX` characters (413), on an
    allowed push service (400 ``push_host_not_allowed``). ``keys.p256dh``: a
    P-256 point on the curve; ``keys.auth``: 16 bytes. ``expirationTime`` is
    ignored: an expired subscription answers 404/410 and is dropped then.
    """
    endpoint = body.get("endpoint")
    if not isinstance(endpoint, str) or not endpoint:
        raise RequestError(400, "invalid", "'endpoint' must be the subscription's push URL")
    if len(endpoint) > PUSH_ENDPOINT_MAX:
        raise RequestError(413, "too_large", f"'endpoint' is over {PUSH_ENDPOINT_MAX} characters")
    if not push_host_allowed(endpoint):
        raise RequestError(
            400,
            "push_host_not_allowed",
            "this server sends pushes only to the browsers' own push services "
            "(Chrome, Firefox, Safari, Edge)",
        )
    keys = body.get("keys")
    if not isinstance(keys, Mapping):
        raise RequestError(400, "invalid", "'keys' must hold the subscription's p256dh and auth")
    p256dh = _push_unb64(keys.get("p256dh"))
    if p256dh is None or not _push_on_curve(p256dh):
        raise RequestError(400, "invalid", "'keys.p256dh' must be a P-256 public key, base64url")
    auth = _push_unb64(keys.get("auth"))
    if auth is None or len(auth) != 16:
        raise RequestError(400, "invalid", "'keys.auth' must be 16 bytes, base64url")
    return PushSubscriptionRecord(endpoint, _push_b64(p256dh), _push_b64(auth), _push_iso(now))


def push_subscribe_device(
    device_id: str, record: PushSubscriptionRecord, live: Collection[str]
) -> None:
    """Make ``record`` this device's one subscription.

    A re-subscription replaces the device's old one. The same endpoint held by
    ANOTHER device moves here: a phone that unlocked again into a new device (a
    new ngrok origin, an installed iOS app) keeps one subscription, not two
    pushes per item. Devices no longer ``live`` lose theirs while the file is open.
    """
    with _push_state_edit() as state:
        _push_prune(state, live)
        for other, held in list(state.subscriptions.items()):
            if held.endpoint == record.endpoint and other != device_id:
                del state.subscriptions[other]
        state.subscriptions[device_id] = record


def push_unsubscribe_device(device_id: str, live: Collection[str]) -> bool:
    """Forget this device's subscription; ``True`` when it had one."""
    with _push_state_edit() as state:
        _push_prune(state, live)
        return state.subscriptions.pop(device_id, None) is not None


# --- sending one push (SPEC §5.5, §5.7) -------------------------------------------------------


def push_https_transport(endpoint: str, headers: dict[str, str], body: bytes) -> int:
    """POST one push to its service: its status, or an exception for a network failure.

    The allowlist is asked again here, whatever the caller checked, because this
    is the one place a request leaves the machine. No redirect is followed and no
    proxy is used (``http.client`` does neither), the certificate is verified,
    the timeout is 10 s, and at most 4 KiB of the answer is read.
    """
    import http.client
    import ssl
    from urllib.parse import urlsplit

    if not push_host_allowed(endpoint):
        raise ValueError(f"not an allowed push service: {endpoint[:80]!r}")
    parts = urlsplit(endpoint)
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    connection = http.client.HTTPSConnection(
        parts.hostname or "",
        443,
        timeout=PUSH_TIMEOUT_SECONDS,
        context=ssl.create_default_context(),
    )
    try:
        connection.request("POST", target, body=body, headers=headers)
        response = connection.getresponse()
        response.read(PUSH_RESPONSE_MAX)
        return response.status
    finally:
        connection.close()


def push_record_outcome(device_id: str, endpoint: str, status: int | None) -> None:
    """Apply a push service's answer to the subscription it was about (SPEC §5.7).

    2xx resets the failures; 404/410 drop it (it expired, or the phone
    unsubscribed); 400/401/403 count, and drop it at the third; 413 is our bug,
    logged and kept; 429, 5xx and no answer at all count and keep it, with no
    retry loop: the next push is the retry. An answer about an endpoint the
    device has since replaced is about nothing any more.
    """
    with _push_state_edit() as state:
        record = state.subscriptions.get(device_id)
        if record is None or record.endpoint != endpoint:
            return
        if status is not None and 200 <= status < 300:
            if record.failures:
                state.subscriptions[device_id] = replace(record, failures=0)
        elif status in (404, 410):
            del state.subscriptions[device_id]
            log.info("remote: %s's push subscription is gone (%s); dropped it", device_id, status)
        elif status == 413:
            log.error(
                "remote: a push to %s was refused as too large; pushes are capped at %d bytes",
                device_id,
                PUSH_PLAINTEXT_MAX,
            )
        elif status in (400, 401, 403) and record.failures + 1 >= PUSH_FAILURES_MAX:
            del state.subscriptions[device_id]
            log.info(
                "remote: %s's push service refused %d times in a row (%s); dropped it",
                device_id,
                PUSH_FAILURES_MAX,
                status,
            )
        else:
            state.subscriptions[device_id] = replace(record, failures=record.failures + 1)


def push_plaintext(message: Mapping[str, object]) -> bytes:
    """The JSON a push carries, at most :data:`PUSH_PLAINTEXT_MAX` bytes.

    Cut before encryption: ids first (the page refetches the feed anyway), then
    the longer of body and title, so an oversized push still says something.
    Every field is bounded, so this only ever matters to a fleet on fire.
    """
    payload = dict(message)
    encoded = _push_json(payload)
    while len(encoded) > PUSH_PLAINTEXT_MAX:
        ids = payload.get("ids")
        longest = max(("body", "title"), key=lambda key: len(str(payload.get(key) or "")))
        text = str(payload.get(longest) or "")
        if isinstance(ids, list) and ids:
            payload["ids"] = ids[: len(ids) // 2]
        elif text:
            payload[longest] = text[: len(text) // 2] + "…" if len(text) > 2 else ""
        elif payload.get("url") is not None:
            payload["url"] = None
        else:
            break  # only our own short fixed keys are left
        encoded = _push_json(payload)
    return encoded


def push_send_one(
    device_id: str,
    record: PushSubscriptionRecord,
    message: Mapping[str, object],
    *,
    keys: VapidKeys,
    transport: PushTransport,
    now: datetime,
) -> int | None:
    """Encrypt ``message`` to one subscription, sign it, send it, and record the answer.

    ``None``: no answer came (a network error, a timeout, or a record that could
    not be encrypted to), which counts as a failure like a 5xx. Never raises: the
    caller is a loop serving every other device.

    The ``Topic`` is the message's tag, so a push service holding a notification
    for an offline phone replaces it only with a newer one of its own kind: a
    later needs push supersedes the stale count, and a farewell never takes the
    place of an unread needs push.
    """
    status: int | None
    try:
        ua_public, auth = _push_unb64(record.p256dh), _push_unb64(record.auth)
        if ua_public is None or auth is None:
            raise ValueError("the stored subscription keys are not base64url")
        headers = {
            "Authorization": vapid_authorization(record.endpoint, keys, now=now),
            "Content-Encoding": "aes128gcm",
            "Content-Type": "application/octet-stream",
            "TTL": str(PUSH_TTL_SECONDS),
            "Urgency": "high",
            "Topic": str(message.get("tag") or "asq-needs"),
        }
        body = encrypt_push_payload(push_plaintext(message), ua_public, auth)
        status = transport(record.endpoint, headers, body)
    except Exception as exc:  # the network, a timeout, a malformed record: one failure each
        log.debug("remote: the push to %s failed: %s", device_id, exc)
        status = None
    else:
        log.debug("remote: the push to %s answered %s", device_id, status)
    try:
        push_record_outcome(device_id, record.endpoint, status)
    except OSError as exc:  # the answer is lost, not the push, nor the next device's
        log.warning("remote: could not record what %s's push service said: %s", device_id, exc)
    return status


# --- what a notification says (SPEC §5.6) -----------------------------------------------------


def push_card_url(base_url: str | None, item: NeedsItem) -> str | None:
    """The link to this item's card: ``<base>#/n/<id>/p/<project id>[/a/<label>]``.

    The page can then say what changed even when it never saw the item. A
    segment the page's router would refuse is left out rather than sent, and an
    item without an agent (a project-level kind) links to its project.
    """
    if base_url is None:
        return None
    if not (_ROUTE_ID.match(item.id) and _ROUTE_SEGMENT.match(item.project_id)):
        return f"{base_url}#/"
    route = f"#/n/{item.id}/p/{item.project_id}"
    if item.agent is not None and _ROUTE_SEGMENT.match(item.agent):
        route += f"/a/{item.agent}"
    return base_url + route


def push_needs_message(
    items: Sequence[NeedsItem], *, total: int, base_url: str | None
) -> PushMessage:
    """The notification for ``items`` (in feed order), the feed holding ``total`` in all.

    One item: ``"<project>: <label> needs you"`` (``"<project> needs you"`` for a
    project-level kind), its reason, and its card. Several: ``"<n> things need
    you"``, the first two reasons, and the feed. `` · <total> open`` when the feed
    holds more than this covers. Every name is cut and cleaned by
    ``needs_push_safe``, and the reasons, needs-you's fixed templates, are cleaned
    again here: nothing an agent typed reaches a lock screen unbounded.
    """
    from aisquare.services import remote_needs

    safe = remote_needs.needs_push_safe
    first = items[0]
    if len(items) == 1:
        project = safe(first.project_name, 40)
        who = f"{project}: {safe(first.agent, 40)}" if first.agent else project
        title, body = f"{who} needs you", safe(first.reason, PUSH_REASON_MAX)
        url = push_card_url(base_url, first)
    else:
        title = f"{len(items)} things need you"
        body = " · ".join(safe(item.reason, PUSH_REASON_MAX) for item in items[:2])
        url = None if base_url is None else f"{base_url}#/"
    if total > len(items):
        title += f" · {total} open"
    ids = [item.id for item in items][:PUSH_IDS_MAX]
    return {"v": 1, "title": title, "body": body, "tag": "asq-needs", "url": url, "ids": ids}


def push_system_message(title: str, body: str, url: str | None, *, tag: str) -> PushMessage:
    """A system push: fixed text under its own ``tag``, so it never replaces a needs push."""
    return {"v": 1, "title": title, "body": body, "tag": tag, "url": url, "ids": []}


# --- one-shot pushes, off the caller's thread -------------------------------------------------


def _push_targets(device_ids: Collection[str]) -> list[tuple[str, PushSubscriptionRecord]]:
    """These devices' subscriptions, read NOW: the caller may be about to revoke them."""
    if push_crypto_missing() is not None:
        return []
    subscriptions = load_push_state().subscriptions
    return [(d, subscriptions[d]) for d in device_ids if d in subscriptions]


_push_in_flight: set[threading.Thread] = set()
"""The one-shot push threads still sending, which :func:`push_drain` waits for."""
_push_in_flight_lock = threading.Lock()


def _push_in_background(
    targets: Sequence[tuple[str, PushSubscriptionRecord]],
    message: PushMessage,
    *,
    transport: PushTransport | None = None,
) -> None:
    """Send ``message`` to ``targets`` from a daemon thread; the caller never waits on it.

    The process does, on its way out (:func:`push_drain`).
    """

    def push_now_thread() -> None:
        try:
            keys = load_push_state().vapid
            if keys is None:
                return
            for device_id, record in targets:
                push_send_one(
                    device_id,
                    record,
                    message,
                    keys=keys,
                    transport=transport or push_https_transport,
                    now=_push_utc_now(),
                )
        except Exception:  # a daemon thread's failure is logged, or it is lost
            log.warning("remote: a push could not be sent", exc_info=True)
        finally:
            with _push_in_flight_lock:
                _push_in_flight.discard(threading.current_thread())

    thread = threading.Thread(target=push_now_thread, name="asq-remote-push-now", daemon=True)
    with _push_in_flight_lock:  # its last step waits for this lock: it is in the set by then
        try:
            thread.start()
        except RuntimeError as exc:  # the interpreter is exiting, or out of threads
            log.warning("remote: a push was not sent: %s", exc)
            return
        _push_in_flight.add(thread)


def push_drain(timeout: float = PUSH_DRAIN_SECONDS) -> bool:
    """Wait at most ``timeout`` s for the one-shot pushes still sending; ``True`` once none is.

    Run at exit (:mod:`atexit`). A one-shot push is sent from a daemon thread so
    that its caller never waits on a push service, but a daemon thread dies
    with its process: ``asq remote serve`` returns a quarter of a second after
    its auto-off queued the farewell, and the TUI can quit right after Remote
    was turned off, both well inside a TLS handshake with the push service.
    Exit handlers run before the interpreter stops its daemon threads, so the
    farewell gets its own timeout to arrive, and a process with nothing in
    flight leaves at once.
    """
    deadline = time.monotonic() + timeout
    with _push_in_flight_lock:
        pending = list(_push_in_flight)
    for thread in pending:
        thread.join(max(0.0, deadline - time.monotonic()))
    return not any(thread.is_alive() for thread in pending)


atexit.register(push_drain)


def push_farewell(
    device_ids: Collection[str], reason: str, *, transport: PushTransport | None = None
) -> None:
    """Tell these devices Remote is off, before they are revoked (SPEC §2.4, §5.6).

    Their subscriptions are read now, while they still exist, and sent to from a
    daemon thread: turning Remote off never waits on a push service, and the
    revoke that follows cannot take the farewell with it. A process that exits
    right after (``serve``'s auto-off) waits for it on the way out
    (:func:`push_drain`). ``reason`` (``remote off``, ``auto-off``) is logged,
    not sent; the sentence is the same either way. Never raises into the caller.
    """
    try:
        targets = _push_targets(device_ids)
    except Exception:
        log.warning("remote: the farewell push could not read its subscriptions", exc_info=True)
        return
    if not targets:
        return
    log.debug("remote: farewell push (%s) to %d devices", reason, len(targets))
    message = push_system_message(FAREWELL_TITLE, FAREWELL_BODY, None, tag="asq-remote-off")
    _push_in_background(targets, message, transport=transport)


def push_security_alert(
    device_ids: Collection[str],
    text: str,
    *,
    transport: PushTransport | None = None,
    now: datetime | None = None,
) -> None:
    """Warn these devices that someone is guessing the password, once per trip (SPEC §2.2).

    The lockout's caller sends the sentence (``text``, its fixed advice to rotate
    the link); this adds the title. A second call inside the same 30 minutes is
    the same trip and sends nothing: ``sys:lockout:<when it began>`` is the key.
    Sent from a daemon thread; never raises into the caller, which is answering
    an unlock.
    """
    at = now or _push_utc_now()
    try:
        targets = _push_targets(device_ids)
        if not targets:
            return
        with _push_state_edit() as state:
            for key, stamp in state.pushed.items():
                began = _push_parse_time(stamp)
                if (
                    key.startswith("sys:lockout:")
                    and began is not None
                    and timedelta(0) <= at - began < LOCKOUT_ALERT_WINDOW
                ):
                    return
            state.pushed[f"sys:lockout:{_push_iso(at)}"] = _push_iso(at)
            state.pushed = _push_prune_pushed(state.pushed, at)
    except Exception:
        log.warning("remote: the lockout alert could not be prepared", exc_info=True)
        return
    message = push_system_message(LOCKOUT_TITLE, text, None, tag="asq-lockout")
    _push_in_background(targets, message, transport=transport)


# --- the sender (SPEC §5.6) -------------------------------------------------------------------

_STOP = object()


class RemotePushSender:
    """The needs pushes and the timed system pushes of one server, on one thread.

    The needs watcher calls :meth:`enqueue_needs_push` after every scan, which only
    queues. The thread (``asq-remote-push``, :meth:`push_sender_loop`) owns every
    decision and every send. Its state machine is two methods, which the thread
    drives and the tests call directly with a fake clock: :meth:`push_scan_seen`
    takes one scan, and :meth:`push_run_due` does whatever has fallen due and says
    how long until the next thing will.

    An item is pushed when its ``push_after`` has passed, two consecutive scans
    showed it, and it is still in the watcher's feed when its coalescing window
    closes. Each subscribed device then gets ONE notification covering every
    such item, no sooner than 20 s after its last, and the ids it covered are
    then recorded as pushed, so neither a later scan nor a restart pushes them
    again. Without a watcher at ``kit.lane_state["needs"]`` nothing is pushed.
    """

    def __init__(
        self,
        kit: RemoteKit,
        *,
        transport: PushTransport | None = None,
        clock: Callable[[], datetime] | None = None,
        discover: Callable[[int, float], str | None] | None = None,
    ) -> None:
        self._kit = kit
        self._transport = transport
        """``None``: :func:`push_https_transport`, looked up at each send."""
        self._clock = clock or _push_utc_now
        self._discover = discover
        """``None``: ``ngrok_tunnel.discover_ngrok_public_url``."""
        self._queue: queue.Queue[object] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._streak: dict[str, int] = {}
        """Per item id, how many consecutive scans showed it. An absence forgets it."""
        self._window: set[str] = set()
        """Ids that became pushable since the coalescing window opened."""
        self._window_closes: datetime | None = None
        self._owed: dict[str, set[str]] = {}
        """Per device id, the item ids its next notification covers, while its throttle runs."""
        self._last_sent: dict[str, datetime] = {}
        """Per device id, when its last needs notification went out."""
        self._pushed: dict[str, str] | None = None
        """``remote-push.json``'s ``pushed``, read once, then kept current by :meth:`_push_mark`."""
        self._next_system_check: datetime | None = None
        self._discovered_at: datetime | None = None
        self._discovered_origin: str | None = None
        """The origin this sender found through ngrok's API and noted, so it can ask again."""
        self._failing = False

    # --- the thread ---

    def push_begin(self) -> None:
        """Start the thread."""
        self._thread = threading.Thread(
            target=self.push_sender_loop, name="asq-remote-push", daemon=True
        )
        self._thread.start()

    def push_end(self, timeout: float = PUSH_STOP_SECONDS) -> None:
        """Stop the thread, waiting at most ``timeout`` for a send in flight."""
        self._queue.put(_STOP)
        if self._thread is not None:
            self._thread.join(timeout)

    def enqueue_needs_push(self, items: list[NeedsItem], scanned_at: datetime) -> None:
        """The needs watcher's listener: queue one scan, and return at once."""
        self._queue.put((list(items), scanned_at))

    def push_sender_loop(self) -> None:
        """Take each scan as it comes, and wake between scans for whatever falls due."""
        wait = 0.0
        while True:
            try:
                message = self._queue.get(timeout=wait)
            except queue.Empty:
                message = None
            if message is _STOP:
                return
            try:
                if isinstance(message, tuple):
                    items, scanned_at = message
                    self.push_scan_seen(items, scanned_at)
                wait = self.push_run_due()
                self._failing = False
            except Exception:  # one bad scan or send must not end every phone's notifications
                level = logging.DEBUG if self._failing else logging.WARNING
                self._failing = True
                log.log(level, "remote: the push sender failed; it carries on", exc_info=True)
                wait = PUSH_SYSTEM_CHECK_SECONDS

    # --- the state machine ---

    def push_scan_seen(self, items: Sequence[NeedsItem], scanned_at: datetime) -> None:
        """One scan: count each item's consecutive scans, and gather what is now pushable."""
        self._streak = {item.id: self._streak.get(item.id, 0) + 1 for item in items}
        # An item gone from this scan leaves the window too; back, it starts over.
        self._window.intersection_update(self._streak)
        pushed = self._push_pushed()
        for item in items:
            if (
                item.push_after is not None
                and item.push_after <= scanned_at
                and self._streak[item.id] >= 2
                and item.id not in pushed
            ):
                self._window.add(item.id)
        if self._window and self._window_closes is None:
            self._window_closes = self._clock() + timedelta(seconds=PUSH_COALESCE_SECONDS)

    def push_run_due(self) -> float:
        """Do what is due now; the seconds until the next thing will be."""
        now = self._clock()
        if self._window_closes is not None and now >= self._window_closes:
            self._push_close_window(now)
        if self._owed:
            self._push_send_owed(now)
        if self._next_system_check is None or now >= self._next_system_check:
            self._next_system_check = now + timedelta(seconds=PUSH_SYSTEM_CHECK_SECONDS)
            self._push_system_checks(now)
        return self._push_next_due(now)

    def deliver_one_push(
        self, device_id: str, record: PushSubscriptionRecord, message: PushMessage
    ) -> int | None:
        """One notification to one device, through this sender's transport and clock.

        The key is read for every send, never kept: a ``remote-push.json`` lost
        while the server runs gets a new key at the next ``GET api/push``, and the
        phones subscribe again against it. A sender still signing with the old
        one is refused (401/403), and drops each new subscription at its third
        refusal.
        """
        keys = load_push_state().vapid
        if keys is None:  # no key: no subscription can have been made against one
            return None
        return push_send_one(
            device_id,
            record,
            message,
            keys=keys,
            transport=self._transport or push_https_transport,
            now=self._clock(),
        )

    def _push_close_window(self, now: datetime) -> None:
        """The window closed: what is still in the feed is owed to every subscribed device.

        With none subscribed it counts as pushed now: a phone that subscribes later
        hears of what happens next, not of what it is already looking at.
        """
        candidates, self._window, self._window_closes = self._window, set(), None
        present = {item.id for item in self._push_feed()}
        pushed = self._push_pushed()
        covered = [i for i in candidates if i in present and i not in pushed]
        if not covered:
            return
        subscriptions = self._push_live_subscriptions()
        if not subscriptions:
            self._push_mark(covered, now)
        for device_id in subscriptions:
            self._owed.setdefault(device_id, set()).update(covered)

    def _push_send_owed(self, now: datetime) -> None:
        """One notification to each device its throttle allows, of what is still in the feed."""
        subscriptions = self._push_live_subscriptions()
        feed = self._push_feed()
        gap = timedelta(seconds=PUSH_MIN_INTERVAL_SECONDS)
        for device_id in list(self._owed):
            record = subscriptions.get(device_id)
            if record is None:  # unsubscribed, or gone, since: owed nothing any more
                del self._owed[device_id]
                continue
            last = self._last_sent.get(device_id)
            if last is not None and now - last < gap:
                continue
            owed = self._owed.pop(device_id)
            items = [item for item in feed if item.id in owed]
            if not items:  # every one of them cleared while the throttle ran
                continue
            base = self._push_public_base(now)
            message = push_needs_message(items, total=len(feed), base_url=base)
            self._last_sent[device_id] = now
            self.deliver_one_push(device_id, record, message)
            self._push_mark([item.id for item in items], now)

    def _push_system_checks(self, now: datetime) -> None:
        subscriptions = self._push_live_subscriptions()
        if subscriptions:
            self._push_auto_off_warning(now, subscriptions)
            self._push_expiry_warnings(now, subscriptions)

    def _push_auto_off_warning(
        self, now: datetime, subscriptions: Mapping[str, PushSubscriptionRecord]
    ) -> None:
        """Ten minutes before auto-off, once per deadline: an extension arms it again.

        The title says the minutes really left: a deadline nearer than ten minutes
        from the start (``serve --auto-off 5``) is warned of at once, with five.
        """
        raw = self._kit.runtime.remote_json().get("auto_off_at")
        deadline = _push_parse_time(raw) if isinstance(raw, str) else None
        if deadline is None or not timedelta(0) < deadline - now <= AUTO_OFF_WARNING:
            return
        key = f"sys:auto-off:{raw}"
        if key in self._push_pushed():
            return
        self._push_mark([key], now)
        base = self._push_public_base(now)
        title = AUTO_OFF_TITLE.format(minutes=math.ceil((deadline - now).total_seconds() / 60))
        message = push_system_message(title, AUTO_OFF_BODY, base, tag="asq-auto-off")
        for device_id, record in subscriptions.items():
            self.deliver_one_push(device_id, record, message)

    def _push_expiry_warnings(
        self, now: datetime, subscriptions: Mapping[str, PushSubscriptionRecord]
    ) -> None:
        """A day before a device's 7-day sign-in ends, to that device alone."""
        for row in self._kit.runtime.device_rows():
            device_id, expires = row.get("id"), row.get("expires_at")
            if not isinstance(device_id, str) or not isinstance(expires, str):
                continue
            record = subscriptions.get(device_id)
            at = _push_parse_time(expires)
            if record is None or at is None or not timedelta(0) < at - now <= EXPIRY_WARNING:
                continue
            key = f"sys:expiry:{device_id}:{expires}"
            if key in self._push_pushed():
                continue
            self._push_mark([key], now)
            base = self._push_public_base(now)
            message = push_system_message(EXPIRY_TITLE, EXPIRY_BODY, base, tag="asq-expiry")
            self.deliver_one_push(device_id, record, message)

    def _push_next_due(self, now: datetime) -> float:
        due = [self._next_system_check or now]
        if self._window_closes is not None:
            due.append(self._window_closes)
        gap = timedelta(seconds=PUSH_MIN_INTERVAL_SECONDS)
        due += [
            now if (last := self._last_sent.get(device_id)) is None else last + gap
            for device_id in self._owed
        ]
        return max(0.0, (min(due) - now).total_seconds())

    # --- what it reads ---

    def _push_feed(self) -> list[NeedsItem]:
        """The watcher's latest items, ranked; none without a watcher."""
        watcher = self._kit.lane_state.get("needs")
        return [] if watcher is None else list(watcher.needs_items_now())

    def _push_live_subscriptions(self) -> dict[str, PushSubscriptionRecord]:
        """Every subscription of a device the runtime still has; the others dropped first."""
        if not load_push_state().subscriptions:
            return {}
        live = push_device_ids(self._kit)  # before the file lock: the runtime takes its own
        with _push_state_edit() as state:
            _push_prune(state, live)
            return dict(state.subscriptions)

    def _push_pushed(self) -> Mapping[str, str]:
        if self._pushed is None:
            self._pushed = dict(load_push_state().pushed)
        return self._pushed

    def _push_mark(self, keys: Collection[str], now: datetime) -> None:
        """Record ``keys`` as pushed: here first, then in the file, for a restart.

        In that order because the file can fail (a full disk): remembered here,
        this process still pushes each item once, where a record kept only in a
        file that cannot be written would push it again every 25 s.
        """
        marks = dict.fromkeys(keys, _push_iso(now))
        self._pushed = _push_prune_pushed({**self._push_pushed(), **marks}, now)
        try:
            with _push_state_edit() as state:
                state.pushed = _push_prune_pushed({**state.pushed, **marks}, now)
        except OSError as exc:
            _push_warn_once(
                "unrecorded",
                "remote: could not record what was pushed in %s (%s); a restart may push it again",
                remote_push_path(),
                exc,
            )

    def _push_public_base(self, now: datetime) -> str | None:
        """Where a link in a push leads, from authoritative sources only (SPEC §5.8).

        What the TUI or ``serve`` announced, as long as it stands; else, when the
        server knows its port, the tunnel to that port ngrok's local API names,
        asked at most once a minute. An origin found that way is asked about again
        a minute later, as though none were known: a hand-started ngrok without a
        static domain comes back on a new URL, and every link would lead to the
        dead one until the server restarted. Never a request's ``Host``: anyone
        who reaches the server writes that, and a push link is where the human
        types the passphrase.
        """
        origin = self._kit.runtime.remote_public_origin()
        port = self._kit.port
        if port is None or (origin is not None and origin != self._discovered_origin):
            return self._kit.kit_public_url()
        quiet = timedelta(seconds=PUSH_DISCOVERY_SECONDS)
        if self._discovered_at is not None and now - self._discovered_at < quiet:
            return self._kit.kit_public_url()
        self._discovered_at = now
        if self._discover is not None:
            found = self._discover(port, 1.0)
        else:
            from aisquare.services.ngrok_tunnel import discover_ngrok_public_url

            found = discover_ngrok_public_url(port, 1.0)
        try:
            checked = None if found is None else check_public_origin(found)
        except ValueError as exc:
            log.debug("remote: ngrok's API named %r, which is no public origin: %s", found, exc)
            checked = None
        # What ngrok says now replaces only what it said before: an origin the TUI or
        # serve announced while it was being asked is theirs, and stays.
        if self._kit.runtime.remote_public_origin() == origin:
            self._kit.runtime.note_public_origin(checked)
            self._discovered_origin = checked
        return self._kit.kit_public_url()


def start_push_sender(kit: RemoteKit) -> Callable[[], None] | None:
    """Start the sender at ``kit.lane_state["push"]`` and hang it on the needs watcher.

    Without ``cryptography`` nothing starts: nothing could be encrypted, and
    ``GET api/push`` already says why. The stopper unhooks the listener, then
    stops the thread, waiting at most 2 s for a send in flight.
    """
    if push_crypto_missing() is not None:
        return None
    sender = RemotePushSender(kit)
    kit.lane_state["push"] = sender
    kit.needs_listeners.append(sender.enqueue_needs_push)
    sender.push_begin()

    def push_stopper() -> None:
        with contextlib.suppress(ValueError):
            kit.needs_listeners.remove(sender.enqueue_needs_push)
        sender.push_end(PUSH_STOP_SECONDS)
        if kit.lane_state.get("push") is sender:
            del kit.lane_state["push"]

    return push_stopper


# --- the routes (SPEC §5.8) -------------------------------------------------------------------


def push_routes(kit: RemoteKit) -> list[BaseRoute]:
    """``GET api/push``, ``POST api/push/subscribe``, ``DELETE api/push/subscription`` and
    ``POST api/push/test``.

    None is write-gated (each is in ``NOT_WRITE_GATED``): they change what this
    device is shown, never the fleet, and a phone that may not write must still
    hear that something needs it. Every change is audited.
    """
    import asyncio
    from urllib.parse import urlsplit

    from starlette.responses import JSONResponse

    tested: dict[str, float] = {}
    """Per device id, when its last test push was queued (``time.monotonic``)."""

    def push_unavailable(problem: str) -> RequestError:
        return RequestError(503, "push_unavailable", problem)

    async def push_status_endpoint(
        request: Request, device: Device, body: dict[str, Any]
    ) -> Response:
        missing = push_crypto_missing()
        if missing is not None:
            unsupported = {"supported": False, "reason": missing}
            return JSONResponse({**unsupported, "vapid_public_key": None, "subscribed": False})

        def push_status_read() -> tuple[VapidKeys, bool]:
            keys = load_or_create_vapid_keys()
            return keys, device.id in load_push_state().subscriptions

        try:
            keys, subscribed = await asyncio.to_thread(push_status_read)
        except (OSError, ImportError) as exc:
            raise push_unavailable(f"the push keys could not be read or made: {exc}") from exc
        return JSONResponse(
            {"supported": True, "vapid_public_key": keys.public_key, "subscribed": subscribed}
        )

    async def push_subscribe_endpoint(
        request: Request, device: Device, body: dict[str, Any]
    ) -> Response:
        missing = push_crypto_missing()
        if missing is not None:
            raise push_unavailable(missing)
        live = push_device_ids(kit)

        def push_subscribe_store() -> PushSubscriptionRecord:
            record = push_subscription_from_body(body, now=_push_utc_now())
            load_or_create_vapid_keys()  # made on the first GET or subscribe (SPEC §5.2)
            push_subscribe_device(device.id, record, live)
            return record

        try:
            record = await asyncio.to_thread(push_subscribe_store)
        except (OSError, ImportError) as exc:
            raise push_unavailable(f"the subscription could not be stored: {exc}") from exc
        kit.kit_audit(device, "push/subscribe", urlsplit(record.endpoint).hostname or "-")
        return JSONResponse({"subscribed": True}, status_code=201)

    async def push_unsubscribe_endpoint(
        request: Request, device: Device, body: dict[str, Any]
    ) -> Response:
        live = push_device_ids(kit)
        try:
            await asyncio.to_thread(push_unsubscribe_device, device.id, live)
        except OSError as exc:
            raise push_unavailable(f"the subscription could not be removed: {exc}") from exc
        kit.kit_audit(device, "push/subscription", "-")
        return JSONResponse({"subscribed": False})

    async def push_test_endpoint(
        request: Request, device: Device, body: dict[str, Any]
    ) -> Response:
        missing = push_crypto_missing()
        if missing is not None:
            raise push_unavailable(missing)
        try:
            state = await asyncio.to_thread(load_push_state)
        except OSError as exc:
            raise push_unavailable(f"the subscriptions could not be read: {exc}") from exc
        record = state.subscriptions.get(device.id)
        if record is None:
            raise RequestError(
                404,
                "not_subscribed",
                "this device has no push subscription — turn notifications on",
            )
        now = time.monotonic()
        last = tested.get(device.id)
        if last is not None and now - last < PUSH_TEST_INTERVAL_SECONDS:
            wait = math.ceil(PUSH_TEST_INTERVAL_SECONDS - (now - last))
            return kit.kit_refuse(
                429,
                "push_test_throttled",
                f"one test push every {PUSH_TEST_INTERVAL_SECONDS:.0f} s — wait {wait} s",
                headers={"Retry-After": str(wait)},
            )
        tested[device.id] = now
        message = push_system_message(TEST_TITLE, TEST_BODY, kit.kit_public_url(), tag="asq-test")
        _push_in_background([(device.id, record)], message)
        kit.kit_audit(device, "push/test", "-")
        return JSONResponse({"queued": True}, status_code=202)

    return [
        kit.kit_route("/api/push", push_status_endpoint, methods=["GET"], write_gated=False),
        kit.kit_route(
            "/api/push/subscribe", push_subscribe_endpoint, methods=["POST"], write_gated=False
        ),
        kit.kit_route(
            "/api/push/subscription",
            push_unsubscribe_endpoint,
            methods=["DELETE"],
            write_gated=False,
        ),
        kit.kit_route("/api/push/test", push_test_endpoint, methods=["POST"], write_gated=False),
    ]


__all__ = [
    "AUTO_OFF_WARNING",
    "EXPIRY_WARNING",
    "PUSH_COALESCE_SECONDS",
    "PUSH_MIN_INTERVAL_SECONDS",
    "PushState",
    "PushSubscriptionRecord",
    "RemotePushSender",
    "VapidKeys",
    "encrypt_push_payload",
    "load_or_create_vapid_keys",
    "load_push_state",
    "push_drain",
    "push_farewell",
    "push_host_allowed",
    "push_routes",
    "push_security_alert",
    "save_push_state",
    "start_push_sender",
    "vapid_authorization",
]
