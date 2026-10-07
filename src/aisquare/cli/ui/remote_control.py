"""The Remote modal's model: server + tunnel lifecycle, the link, and the switches' autosave.

The modal (``views/remote.py``) is a view over ONE ``RemoteController`` that lives
on the app, so closing and reopening the dialog never restarts a tunnel and the
auto-off timer keeps counting with the dialog closed. Nothing here imports
Textual, so every branch — on/off, ngrok missing, auto-off, restore after a
restart — is a plain unit test.

Persistence (PLAN §1): the two switch values ``remote_enabled`` and
``auto_off_minutes`` sit next to the theme key in ``~/.aisquare/state.json``
through the file's one locked writer (``core.state_file.update_state``), as the
theme autosave does. Token, password, devices and the write switch are the
SERVER's (``~/.aisquare/remote.json``) and are only read and written through its
API, so ``aisquare remote allow-write`` and ``regenerate-password`` from a shell
and this modal always agree.

Write actions default OFF and this module never flips them on its own: the only
path to ``allow_write=True`` is the user's switch.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

from aisquare.core.state_file import StateUnwritableError, read_state, update_state
from aisquare.services import remote_server
from aisquare.services.ngrok_tunnel import NgrokTunnel, build_public_url

# The one sentence every read-only refusal says, re-exported for the modal.
from aisquare.services.remote_server import READ_ONLY_REASON as READ_ONLY_REASON

AUTO_OFF_CHOICES: tuple[int | None, ...] = (30, 60, 120, None)
"""Minutes until Remote switches itself off — ``None`` is "Never".

Never is a DELIBERATE choice, never the resting posture: the page is publicly
tunnelled, so a session the human forgets about is a session anyone with the
link can keep reaching. The default stays :data:`DEFAULT_AUTO_OFF`, and only an
explicit pick (persisted as :data:`NEVER`) switches the timer off."""
DEFAULT_AUTO_OFF = 60
NEVER = "never"
"""How Never is stored under ``auto_off_minutes`` in ``state.json``."""
STATE_KEYS = ("remote_enabled", "auto_off_minutes")

TunnelFactory = Callable[[int], NgrokTunnel]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _aware(at: datetime) -> datetime:
    """``at`` with its offset: a naive time is read as this machine's local time."""
    return at if at.tzinfo is not None else at.astimezone()


@dataclass(frozen=True)
class RemoteState:
    """The switch values that survive a restart of the TUI.

    Not the write switch: that is ``remote.json``'s alone. A copy here was a second
    source of truth that ``turn_on`` pushed into the server, so a TUI start undid an
    ``aisquare remote allow-write off`` from a shell, and the modal showed read-only
    while the server took writes.
    """

    remote_enabled: bool = False
    auto_off_minutes: int | None = DEFAULT_AUTO_OFF
    """``None`` = Never; see :data:`AUTO_OFF_CHOICES`."""


def load_remote_state() -> RemoteState:
    """The saved switches; a missing or malformed key falls back to its (safe) default."""
    data = read_state()
    # The fallback is the DEFAULT, not ``None``: an absent key is a fresh machine
    # and must mean 60 minutes, while ``"never"`` is the human having chosen
    # Never. ``update_state`` drops a key set to ``None``, so Never cannot be
    # stored as null; a null an earlier build wrote still reads as Never.
    minutes = data.get("auto_off_minutes", DEFAULT_AUTO_OFF)
    if minutes == NEVER:
        minutes = None
    return RemoteState(
        remote_enabled=data.get("remote_enabled") is True,
        auto_off_minutes=minutes if minutes in AUTO_OFF_CHOICES else DEFAULT_AUTO_OFF,
    )


def save_remote_state(state: RemoteState) -> None:
    """Persist the two switches; a state.json that cannot be written keeps the old ones."""
    minutes = NEVER if state.auto_off_minutes is None else state.auto_off_minutes
    try:
        update_state("remote_enabled", state.remote_enabled)
        update_state("auto_off_minutes", minutes)
    except StateUnwritableError:
        return


class RemoteController:
    """Turn Remote on and off, and answer what the modal paints.

    ``server`` is :mod:`aisquare.services.remote_server`, ``tunnel_factory``
    builds the ngrok subprocess wrapper, ``now`` is the clock — all three are
    seams for the tests. Every time here carries its offset; a naive one from
    ``now`` is read as local time.
    """

    def __init__(
        self,
        *,
        server: ModuleType = remote_server,
        tunnel_factory: TunnelFactory = NgrokTunnel,
        dist_dir: Path | None = None,
        port: int = remote_server.DEFAULT_PORT,
        now: Callable[[], datetime] = _utc_now,
        state: RemoteState | None = None,
        url_timeout: float = 15.0,
    ) -> None:
        self._server = server
        self._tunnel_factory = tunnel_factory
        self._dist_dir = dist_dir
        self._port = port
        self._now = now
        self._url_timeout = url_timeout
        self.state = state if state is not None else load_remote_state()
        self.info: remote_server.RemoteInfo | None = None
        self.tunnel: NgrokTunnel | None = None
        self.public_url: str | None = None
        """``https://<ngrok-host>/r/<token>`` once the tunnel announced itself."""
        self.message: str | None = None
        """What the status line says: waiting for ngrok, the install hint, an error."""
        self.auto_off_at: datetime | None = None
        self._waiter: threading.Thread | None = None

    # --- on / off -----------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self.info is not None

    def turn_on(self) -> None:
        """Start the server, then the tunnel; the public URL arrives on a background thread."""
        if self.running:
            return
        self.public_url = None
        try:
            self.info = self._server.start_remote_server(self._dist_dir, port=self._port)
        except Exception as exc:  # the remote extra is missing, or the port is taken
            # RemoteUnavailable / RemoteError carry the sentence to show; Remote stays
            # off and the saved switch is not flipped, so a restart does not retry blindly.
            self.info = None
            self.message = f"Remote could not start — {exc}"
            return
        # The write switch is not touched: it is remote.json's, as the shell left it.
        self._arm_auto_off()
        self.message = None
        self._set_state(remote_enabled=True)
        tunnel = self._tunnel_factory(self._port)
        failure = tunnel.start_tunnel()
        if failure is not None:
            # No tunnel, but the local server is up: the modal keeps the local link
            # (PLAN §6 fallback) and the status line says what to install.
            self.message = failure
            self.tunnel = None
            return
        self.tunnel = tunnel
        self.message = "starting ngrok…"
        self._waiter = threading.Thread(
            target=self._await_url, args=(tunnel,), name="ngrok-url", daemon=True
        )
        self._waiter.start()

    def _await_url(self, tunnel: NgrokTunnel) -> None:
        url = tunnel.wait_for_url(self._url_timeout)
        if tunnel is not self.tunnel:  # turned off (or restarted) while we waited
            return
        if url is not None and self.info is not None:
            self.public_url = build_public_url(url, self.info.token)
            self.message = None
            self._note_public_url(self.public_url)
        else:
            self.message = tunnel.error or "ngrok did not announce a tunnel in time"

    def turn_off(self, *, persist: bool = True, reason: str = "remote off") -> None:
        """Stop the server and the tunnel. ``persist=False`` keeps the saved switch (app exit).

        Turning Remote off (the switch, auto-off) revokes every device after the
        farewell push, so no phone keeps a cookie for a Remote that is off: their
        sockets close with 4410, which the page reads as "Remote is off", and the
        tunnel goes last so those closes can still reach them. Leaving the TUI
        revokes nothing: ``restore()`` brings Remote back at the next start, and
        the devices' own expiry bounds them meanwhile.

        Nothing the server raises keeps Remote on. Each step runs whatever the one
        before it raised, the first failure is the status line's sentence, and
        ngrok stops and the controller reads off in any case: auto-off calls this
        from a Textual timer, where an exception ends the whole fleet UI, and a
        ``remote.json`` that would not write once left ngrok up and the switch on.
        """
        tunnel, self.tunnel = self.tunnel, None
        failure: str | None = None
        try:
            if self.info is not None:
                if persist:
                    try:
                        self._server.revoke_every_remote_device(reason)
                    except Exception as exc:  # remote.json unwritable: Remote still goes off
                        failure = f"Remote is off, but its devices could not be revoked — {exc}"
                try:
                    self._server.note_public_url(None)
                    self._server.set_auto_off(None)
                except Exception as exc:  # a deadline left in the file ends nothing
                    failure = failure or f"Remote is off, but remote.json was not updated — {exc}"
                try:
                    self._server.stop_remote_server()
                except Exception as exc:
                    failure = failure or f"Remote is off, but stopping its server failed — {exc}"
        finally:
            if tunnel is not None:
                tunnel.stop_tunnel()
            self.info = None
            self.public_url = None
            self.auto_off_at = None
            self.message = failure
            if persist:
                self._set_state(remote_enabled=False)

    def restore(self) -> None:
        """At TUI start: a Remote that was on when the TUI last exited comes back on."""
        if self.state.remote_enabled and not self.running:
            self.turn_on()

    def shutdown_for_exit(self) -> None:
        """At TUI exit: end the processes, keep the saved switches for ``restore``."""
        self.turn_off(persist=False)

    # --- the controls ---------------------------------------------------------------------

    def write_actions_allowed(self) -> bool:
        """The write switch as ``remote.json`` holds it, Remote on or off; off when unreadable."""
        try:
            return bool(self._server.remote_allow_write())
        except Exception:  # a remote.json that cannot be read shows writes as off
            return False

    def set_allow_write(self, enabled: bool) -> None:
        """Flip the one write switch, in ``remote.json``, whether Remote is on or not: the
        TUI's next Remote and ``asq remote serve`` both start with what it says."""
        try:
            self._server.set_allow_write(bool(enabled))
        except Exception as exc:  # an unwritable remote.json is a sentence, not a crash
            self.message = f"write actions could not be changed — {exc}"

    def set_auto_off(self, minutes: int | None) -> None:
        """Pick a timer, or ``None`` for Never. Takes effect at once while Remote is on."""
        if minutes not in AUTO_OFF_CHOICES:
            raise ValueError(f"auto-off must be one of {AUTO_OFF_CHOICES}, not {minutes}")
        self._set_state(auto_off_minutes=minutes)
        if self.running:
            self._arm_auto_off()

    def regenerate_password(self) -> str | None:
        """A new passphrase from the server; ``None`` while Remote is off (nothing to unlock)."""
        if self.info is None:
            return None
        return str(self._server.regenerate_password())

    def _server_status(self) -> dict[str, Any]:
        try:
            status = self._server.remote_server_status()
        except Exception:  # a half-written remote.json costs the list, not the modal
            return {}
        return status if isinstance(status, dict) else {}

    def devices(self) -> list[dict[str, Any]]:
        """The devices ``[{id, ua, first_seen, last_seen, expires_at, signed_in}]``, by id."""
        rows = self._server_status().get("devices", [])
        rows = rows if isinstance(rows, list) else []
        return [row for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)]

    def revoke_device(self, device_id: str) -> None:
        self._server.revoke_remote_device(device_id)

    def unlock_failures(self) -> tuple[int, str | None]:
        """Wrong passphrases in the last 30 min, and until when new unlocks are paused."""
        status = self._server_status()
        failed, until = status.get("failed_unlocks"), status.get("locked_out_until")
        return (
            failed if isinstance(failed, int) else 0,
            until if isinstance(until, str) and until else None,
        )

    # --- what the modal paints ---------------------------------------------------------------

    def link_url(self) -> str | None:
        """The public link when ngrok is up, else the local one — ``None`` while Remote is off."""
        if self.info is None:
            return None
        return self.public_url or self.info.url_local

    def password(self) -> str | None:
        """The passphrase as ``remote.json`` says now: a ``regenerate-password`` from a shell
        shows at the next paint, not the one this Remote started with. ``None`` while off."""
        if self.info is None:
            return None
        try:
            return str(self._server.remote_password())
        except Exception:  # unreadable for a moment: the one in hand beats a blank
            return self.info.password

    # --- auto-off -------------------------------------------------------------------------------

    def _arm_auto_off(self) -> None:
        """Set (or clear, for Never) the deadline; the server reports it as ``auto_off_at``.

        An instant with its offset, from an aware clock: naive local time plus an
        hour ran an hour long across a DST fall-back, and was published without an
        offset a phone elsewhere read in its own timezone.
        """
        minutes = self.state.auto_off_minutes
        now = _aware(self._now())
        self.auto_off_at = None if minutes is None else now + timedelta(minutes=minutes)
        # The server shows it as GET /api/remote's auto_off_at (PLAN §4-B); Never is
        # null there, which is the same thing it shows while Remote is off.
        self._server.set_auto_off(self.auto_off_at)

    def adopt_server_deadline(self) -> datetime | None:
        """The deadline, moved to the server's when that is later: a phone extended it.

        Called by :meth:`enforce_auto_off` and on every paint, so the extension holds
        and the modal shows the new time. The server enforces it either way.
        """
        if self.running and self.auto_off_at is not None:
            try:
                served = self._server.remote_auto_off_at()
            except Exception:  # unreadable for a moment: the deadline in hand still stands
                served = None
            if served is not None and _aware(served) > self.auto_off_at:
                self.auto_off_at = _aware(served)
        return self.auto_off_at

    def enforce_auto_off(self) -> bool:
        """Turn Remote off when its timer has run out; ``True`` when it just did."""
        deadline = self.adopt_server_deadline()
        if self.running and deadline is not None and _aware(self._now()) >= deadline:
            self.turn_off(reason="auto-off")
            ran_out = "Remote turned off — the auto-off timer ran out"
            # What turning off could not do still shows: a revoke that failed leaves phones
            # holding cookies the next Remote accepts.
            self.message = ran_out if self.message is None else f"{ran_out}. {self.message}"
            return True
        return False

    # --- persistence -----------------------------------------------------------------------------

    def _set_state(self, **changes: Any) -> None:
        self.state = replace(self.state, **changes)
        save_remote_state(self.state)

    # --- the tunnel watchdog ---------------------------------------------------------------------

    REVIVE_SECONDS = 60.0
    """A dead tunnel is started again at most this often."""
    _revived_at: datetime | None = None
    """When :meth:`revive_tunnel_if_dead` last started ngrok."""
    _revived_tunnel: NgrokTunnel | None = None
    """The tunnel the last revive started. Should it die before it announces a URL, it is
    revived all the same: a tunnel of this Remote did come up before it."""
    _link_before_revive: str | None = None
    """The public link the Remote had when its tunnel died, so the message can say whether
    the new one differs, however many restarts it took to get one."""

    def revive_tunnel_if_dead(self) -> bool:
        """Start ngrok again when it died under a Remote that is still on; ``True`` when it did.

        The app runs this every 30 s. Without it, a tunnel that died in the night
        left the server up and unreachable, and the phone's link dead until
        someone at the desk noticed. The FIRST tunnel of a Remote, when it never
        announced a URL, is left alone: it failed for a reason the status line
        already shows (no authtoken, an ngrok too old for ``--url``), and would
        fail the same way again. Once one came up, every dead tunnel is revived,
        a restarted one that died before announcing included: a static domain
        still held by the session that just died (``ERR_NGROK_334``), or a network
        that is down for a while, clears within minutes, and the watchdog must
        still be trying then. The new link is shown, and noted for push links, as
        soon as ngrok announces it.
        """
        dead = self.tunnel
        if not self.running or dead is None or dead.running:
            return False
        if dead.public_url is None and dead is not self._revived_tunnel:
            return False
        now = self._now()
        if self._revived_at is not None and now - self._revived_at < timedelta(
            seconds=self.REVIVE_SECONDS
        ):
            return False
        self._revived_at = now
        tunnel = self._tunnel_factory(self._port)
        failure = tunnel.start_tunnel()
        if failure is not None:  # the dead one stays, so the next minute tries again
            self.message = failure
            return False
        dead.stop_tunnel()
        if self.public_url is not None:  # else the last restart never came up: keep the link
            self._link_before_revive = self.public_url
        self.tunnel = self._revived_tunnel = tunnel
        self.public_url = None  # the modal shows the local link until ngrok announces one
        self.message = "ngrok stopped — restarting it…"
        self._waiter = threading.Thread(
            target=self._await_revived_url,
            args=(tunnel, self._link_before_revive),
            name="ngrok-url",
            daemon=True,
        )
        self._waiter.start()
        return True

    def _await_revived_url(self, tunnel: NgrokTunnel, dead_link: str | None) -> None:
        """:meth:`_await_url`, then say ngrok was restarted, and whether the link changed.

        A restart that dies before it announces leaves :meth:`_await_url`'s
        message, ngrok's own error, on the status line until the next one.
        """
        self._await_url(tunnel)
        if tunnel is self.tunnel and self.public_url is not None:
            changed = "" if self.public_url == dead_link else "; the link changed"
            self.message = f"ngrok stopped — restarted it{changed}"

    def _note_public_url(self, url: str) -> None:
        """Tell the server where phones reach it, so push links lead there (SPEC §5.8).

        Only an https URL on a DNS name is an origin: a tunnel that announces
        anything else leaves push links without one, rather than raising in the
        thread that waited for it.
        """
        with contextlib.suppress(ValueError):
            self._server.note_public_url(url)
