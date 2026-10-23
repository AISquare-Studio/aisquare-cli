"""The owner gate (#216): a code from the owner's authenticator opens a window to act as them.

The captain presses keys into panes, pastes, stops, spawns: it acts as the owner. With
no gate, anyone at the terminal, or a misheard request, could too. While the captain is on
and ``[captain] gate`` is not ``false`` (the default is on), the privileged tools
(:data:`PRIVILEGED`) refuse outside an *act-as-owner window*. A TOTP code opens one for
:data:`WINDOW`: spoken to the captain (its ``unlock`` tool) or typed (``aisquare captain
gate unlock``). Reads never need one, and nor do ``tell`` and ``ask_manager`` (an owner
call: they spend quota, but each only types into one agent or asks the manager).

**RFC 6238 and RFC 4226.**
- A code is a 30-second TOTP step, and at most one step of drift is allowed either side.
- A code that was accepted is never accepted again, nor one from an earlier step: the
  last accepted step is kept.
- Failed attempts are throttled (RFC 4226 §7.3). Every :data:`THROTTLE`-th wrong code in a
  row locks code entry for :data:`LOCKOUT` and says so: the refusal is audited on the
  captain's home board, which is where "three wrong codes in a row" shows.

**Where things live.**
- The secret is ``~/.aisquare/captain/gate.secret``, mode 0600, written once by ``aisquare
  captain gate enroll``.
- The window, the last accepted step and the failure count live in ``state.json``, under
  its lock (``core.state_file``). The captain's Actions server and the owner's CLI are two
  processes and read one window.
- The window's id joins every privileged ``captain_action`` audit event.

A spoken code can be overheard; the docs say so beside the gate (``docs/captain.md``).

The verifier is a protocol (:class:`Verifier`) so a biometric adapter can sit behind it
later. The one shipped here is TOTP through ``pyotp``, in the ``gate`` extra, never core.
Without it, or before enrolment, the gate fails closed: the privileged tools refuse and
say how to fix it.
"""

from __future__ import annotations

import contextlib
import hmac
import os
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, cast

from aisquare.core import experimental, state_file
from aisquare.services.captain import speaker
from aisquare.services.captain import state as captain_state
from aisquare.services.captain.errors import Refused

PRIVILEGED: frozenset[str] = frozenset(
    {"press", "paste", "stop", "spawn", "restart", "wololo", "attach_persona", "act"}
)
"""The tools that act as the owner, and so need an open window."""
WINDOW = timedelta(minutes=15)
"""How long one accepted code lets the captain act as the owner."""
STEP_SECONDS = 30
"""RFC 6238's recommended time step."""
DRIFT_STEPS = 1
"""RFC 6238: "at most one time step is allowed as the network delay"."""
THROTTLE = 3
"""RFC 4226 §7.3's throttling parameter: this many wrong codes in a row lock code entry."""
LOCKOUT = timedelta(minutes=5)
"""How long code entry stays locked after every :data:`THROTTLE`-th wrong code in a row."""
ISSUER = "aisquare captain"
"""What an authenticator app shows above the code."""
EXTRA = "pip install 'aisquare-cli[gate]'"
"""How to get pyotp, said where its absence refuses."""
ENROLL = "aisquare captain gate enroll"
UNLOCK = "say your authenticator's code to the captain, or run: aisquare captain gate unlock <code>"
_STATE_KEY = "captain_gate"


class Verifier(Protocol):
    """What the gate asks of an identity check (TOTP here; Touch ID or Hello later)."""

    def available(self) -> str | None:
        """``None`` when it can verify a code now; else why not, with the fix."""

    def matched_step(self, code: str, now: float) -> int | None:
        """The time step ``code`` is valid for at ``now`` (within the drift), or ``None``."""


def secret_path() -> Path:
    return captain_state.captain_dir() / "gate.secret"


class TotpVerifier:
    """RFC 6238 TOTP through pyotp: six digits, 30-second steps, SHA-1 (what every
    authenticator app reads from an ``otpauth://`` URI by default)."""

    def available(self) -> str | None:
        try:
            import pyotp  # noqa: F401 — the gate extra; imported only to ask
        except ImportError:
            return f"the owner gate needs pyotp: {EXTRA} (or set [captain] gate = false)"
        if not secret_path().is_file():
            return f"the owner gate is not enrolled yet: {ENROLL}"
        return None

    def matched_step(self, code: str, now: float) -> int | None:
        import pyotp

        totp = pyotp.TOTP(_read_secret(), interval=STEP_SECONDS)
        step = int(now // STEP_SECONDS)
        # The current step first, then the drift either side (RFC 6238 §5.2).
        offsets = [0, *range(-DRIFT_STEPS, 0), *range(1, DRIFT_STEPS + 1)]
        for candidate in (step + offset for offset in offsets):
            expected: str = totp.at(candidate * STEP_SECONDS)
            if hmac.compare_digest(expected, code):
                return int(candidate)
        return None


VERIFIER: Verifier = TotpVerifier()
"""The gate's identity check, looked up at call time, so a test can replace it."""


@dataclass(frozen=True)
class Window:
    """An open act-as-owner window."""

    id: str
    until: datetime


@dataclass(frozen=True)
class Enrollment:
    """What ``enroll`` wrote: the secret and the URI an authenticator app scans."""

    secret: str
    uri: str
    path: Path


def configured() -> bool:
    """``[captain] gate``: on unless it is literally ``false``. A typo is never off."""
    return speaker.captain_table().get("gate", True) is not False


def active() -> bool:
    """Whether the gate applies now: the captain is on and the gate not switched off."""
    return experimental.captain_enabled() and configured()


def admit(tool: str, *, now: datetime | None = None) -> str | None:
    """The open window's id when ``tool`` may act as the owner; ``None`` when the gate does
    not apply to it (a read, ``tell``, or the gate off). Raises :class:`Refused`, with how
    to unlock, when it applies and no window is open."""
    if tool not in PRIVILEGED or not active():
        return None
    window = current_window(now=now)
    if window is not None:
        return window.id
    why = VERIFIER.available()
    raise Refused(f"{tool} acts as the owner and the gate is locked — {why or UNLOCK}")


def current_window(*, now: datetime | None = None) -> Window | None:
    """The open window, if one is."""
    moment = now or datetime.now(UTC)
    record = _record(state_file.read_state().get(_STATE_KEY))
    window_id, until = record.get("window"), _time(record.get("until"))
    if isinstance(window_id, str) and until is not None and until > moment:
        return Window(window_id, until)
    return None


def unlock(code: str, *, now: datetime | None = None) -> Window:
    """Open a window with a code from the owner's authenticator. Raises :class:`Refused`:
    the gate is off, it cannot verify yet, entry is locked, or the code is wrong or used."""
    if not active():
        raise Refused("the owner gate is off ([captain] gate = false): nothing to unlock")
    why = VERIFIER.available()
    if why is not None:
        raise Refused(why)
    moment = now or datetime.now(UTC)
    digits = re.sub(r"\D", "", code)  # spoken as "123 456", typed as "123-456"
    verdict: list[Window | str] = []

    def change(current: object | None) -> object:
        record = _record(current)
        locked = _time(record.get("locked_until"))
        failures = _count(record.get("failures"))
        if locked is not None and locked > moment:
            verdict.append(
                f"code entry is locked until {locked:%H:%M:%S} UTC after {failures} wrong "
                "codes in a row — wait, then try a fresh code"
            )
            return record
        step = VERIFIER.matched_step(digits, moment.timestamp()) if len(digits) == 6 else None
        last = record.get("last_step")
        if step is not None and isinstance(last, int) and step <= last:
            reason = "that code was already used — wait for the next one"
            step = None
        else:
            reason = "wrong code"
        if step is None:
            failures += 1
            record["failures"] = failures
            if failures % THROTTLE == 0:
                record["locked_until"] = (moment + LOCKOUT).isoformat()
                verdict.append(
                    f"{reason}: a wrong code {failures} times in a row, so code entry is "
                    f"locked for {int(LOCKOUT.total_seconds() // 60)} minutes"
                )
            else:
                verdict.append(f"{reason} ({failures} in a row; {THROTTLE} lock code entry)")
            return record
        window = Window(f"w-{secrets.token_hex(4)}", moment + WINDOW)
        verdict.append(window)
        return {"window": window.id, "until": window.until.isoformat(), "last_step": step}

    state_file.modify_state(_STATE_KEY, change)
    (outcome,) = verdict
    if isinstance(outcome, str):
        raise Refused(outcome)
    return outcome


def lock() -> bool:
    """Close the open window now; ``True`` when one was open. The step and the failure
    count stay, so a used code is still refused."""
    closed: list[bool] = []

    def change(current: object | None) -> object | None:
        record = _record(current)
        closed.append("window" in record)
        record.pop("window", None)
        record.pop("until", None)
        return record or None

    state_file.modify_state(_STATE_KEY, change)
    return closed[0]


def enroll(*, replace: bool = False) -> Enrollment:
    """Write a new secret (mode 0600) and answer the ``otpauth://`` URI to scan. Refuses over
    an enrolled one unless ``replace``, which also forgets every window and accepted step."""
    try:
        import pyotp
    except ImportError as exc:
        raise Refused(f"the owner gate needs pyotp: {EXTRA}") from exc
    path = secret_path()
    if path.exists() and not replace:
        raise Refused(
            f"the owner gate is enrolled already ({path}); {ENROLL} --replace makes a new one"
        )
    secret = pyotp.random_base32()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(descriptor, f"{secret}\n".encode("ascii"))
    finally:
        os.close(descriptor)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    state_file.modify_state(_STATE_KEY, lambda _current: None)
    uri = pyotp.TOTP(secret, interval=STEP_SECONDS).provisioning_uri(
        name="owner", issuer_name=ISSUER
    )
    return Enrollment(secret=secret, uri=uri, path=path)


def status(*, now: datetime | None = None) -> dict[str, object]:
    """What the gate is doing now, for ``gate status`` and ``--json``: never the secret."""
    window = current_window(now=now)
    record = _record(state_file.read_state().get(_STATE_KEY))
    return {
        "active": active(),
        "configured": configured(),
        "captain": experimental.captain_enabled(),
        "enrolled": secret_path().is_file(),
        "ready": VERIFIER.available() is None,
        "why_not": VERIFIER.available(),
        "window": window.id if window is not None else None,
        "until": window.until.isoformat() if window is not None else None,
        "failures": _count(record.get("failures")),
        "locked_until": record.get("locked_until"),
    }


def redact(text: str) -> str:
    """``text`` with anything that could be a code masked: what an ``unlock``'s audit keeps."""
    return re.sub(r"\d(?:[\s-]?\d){5}", "******", text)


def _read_secret() -> str:
    return secret_path().read_text(encoding="ascii").strip()


def _record(value: object | None) -> dict[str, object]:
    return dict(cast(dict[str, object], value)) if isinstance(value, dict) else {}


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    with contextlib.suppress(ValueError):
        stamp = datetime.fromisoformat(value)
        return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=UTC)
    return None
