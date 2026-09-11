"""The Remote modal's model: server + tunnel lifecycle, the link, and the switches' autosave.

The modal (``views/remote.py``) is a view over ONE ``RemoteController`` that lives
on the app, so closing and reopening the dialog never restarts a tunnel and the
auto-off timer keeps counting with the dialog closed. Nothing here imports
Textual, so every branch — on/off, ngrok missing, auto-off, restore after a
restart — is a plain unit test.

Persistence (PLAN §1): the three switch values ``remote_enabled``,
``allow_write`` and ``auto_off_minutes`` sit next to the theme key in
``~/.aisquare/state.json`` through the same merge-and-rename recipe the theme
autosave uses (``cli.watch._update_state``). Token, password and sessions are
the SERVER's (``~/.aisquare/remote.json``) and are only read through its API.

Write actions default OFF and this module never flips them on its own: the only
path to ``allow_write=True`` is the user's switch.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

from aisquare.cli.watch import _read_state, _update_state
from aisquare.services import remote_server
from aisquare.services.ngrok_tunnel import NgrokTunnel, build_public_url

AUTO_OFF_CHOICES: tuple[int, ...] = (30, 60, 120)
DEFAULT_AUTO_OFF = 60
STATE_KEYS = ("remote_enabled", "allow_write", "auto_off_minutes")
READ_ONLY_REASON = "read-only build (allow write actions is off in the TUI)"

TunnelFactory = Callable[[int], NgrokTunnel]


@dataclass(frozen=True)
class RemoteState:
    """The switch values that survive a restart of the TUI."""

    remote_enabled: bool = False
    allow_write: bool = False
    auto_off_minutes: int = DEFAULT_AUTO_OFF


def load_remote_state() -> RemoteState:
    """The saved switches; a missing or malformed key falls back to its (safe) default."""
    data = _read_state()
    minutes = data.get("auto_off_minutes")
    return RemoteState(
        remote_enabled=data.get("remote_enabled") is True,
        allow_write=data.get("allow_write") is True,
        auto_off_minutes=minutes if minutes in AUTO_OFF_CHOICES else DEFAULT_AUTO_OFF,
    )


def save_remote_state(state: RemoteState) -> None:
    _update_state(
        {
            "remote_enabled": state.remote_enabled,
            "allow_write": state.allow_write,
            "auto_off_minutes": state.auto_off_minutes,
        }
    )


class RemoteController:
    """Turn Remote on and off, and answer what the modal paints.

    ``server`` is the PLAN §4-F module (the stub until the server branch
    merges), ``tunnel_factory`` builds the ngrok subprocess wrapper, ``now`` is
    the clock — all three are seams for the tests.
    """

    def __init__(
        self,
        *,
        server: ModuleType = remote_server,
        tunnel_factory: TunnelFactory = NgrokTunnel,
        dist_dir: Path | None = None,
        port: int = remote_server.DEFAULT_PORT,
        now: Callable[[], datetime] = datetime.now,
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
        self.info = self._server.start(self._dist_dir, port=self._port)
        # The server owns allow_write at request time; hand it the saved switch so the
        # two never disagree (and a fresh state hands it False).
        self._server.set_allow_write(self.state.allow_write)
        self._arm_auto_off()
        self.public_url = None
        self.message = None
        self._set_state(remote_enabled=True)
        tunnel = self._tunnel_factory(self._port)
        failure = tunnel.start()
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
        else:
            self.message = tunnel.error or "ngrok did not announce a tunnel in time"

    def turn_off(self, *, persist: bool = True) -> None:
        """Stop the tunnel and the server. ``persist=False`` keeps the saved switch (app exit)."""
        tunnel, self.tunnel = self.tunnel, None
        if tunnel is not None:
            tunnel.stop()
        if self.info is not None:
            self._server.stop()
        self.info = None
        self.public_url = None
        self.auto_off_at = None
        self.message = None
        if persist:
            self._set_state(remote_enabled=False)

    def restore(self) -> None:
        """At TUI start: a Remote that was on when the TUI last exited comes back on."""
        if self.state.remote_enabled and not self.running:
            self.turn_on()

    def shutdown(self) -> None:
        """At TUI exit: end the processes, keep the saved switches for ``restore``."""
        self.turn_off(persist=False)

    # --- the controls ---------------------------------------------------------------------

    def set_allow_write(self, enabled: bool) -> None:
        self._set_state(allow_write=bool(enabled))
        if self.running:
            self._server.set_allow_write(bool(enabled))

    def set_auto_off(self, minutes: int) -> None:
        if minutes not in AUTO_OFF_CHOICES:
            raise ValueError(f"auto-off must be one of {AUTO_OFF_CHOICES}, not {minutes}")
        self._set_state(auto_off_minutes=minutes)
        if self.running:
            self._arm_auto_off()

    def regenerate_password(self) -> str | None:
        """A new passphrase from the server; ``None`` while Remote is off (nothing to unlock)."""
        if self.info is None:
            return None
        password = str(self._server.regenerate_password())
        self.info = remote_server.RemoteInfo(self.info.token, password, self.info.url_local)
        return password

    def devices(self) -> list[dict[str, Any]]:
        """The connected sessions ``[{sid, ua, first_seen, last_seen}]`` from the server."""
        try:
            sessions = self._server.status().get("sessions", [])
        except Exception:  # a half-written remote.json costs the list, not the modal
            return []
        return [s for s in sessions if isinstance(s, dict) and isinstance(s.get("sid"), str)]

    def revoke(self, sid: str) -> None:
        self._server.revoke(sid)

    # --- what the modal paints ---------------------------------------------------------------

    def link_url(self) -> str | None:
        """The public link when ngrok is up, else the local one — ``None`` while Remote is off."""
        if self.info is None:
            return None
        return self.public_url or self.info.url_local

    def password(self) -> str | None:
        return self.info.password if self.info is not None else None

    # --- auto-off -------------------------------------------------------------------------------

    def _arm_auto_off(self) -> None:
        self.auto_off_at = self._now() + timedelta(minutes=self.state.auto_off_minutes)

    def enforce_auto_off(self) -> bool:
        """Turn Remote off when its timer has run out; ``True`` when it just did."""
        if self.running and self.auto_off_at is not None and self._now() >= self.auto_off_at:
            self.turn_off()
            self.message = "Remote turned off — the auto-off timer ran out"
            return True
        return False

    # --- persistence -----------------------------------------------------------------------------

    def _set_state(self, **changes: Any) -> None:
        self.state = replace(self.state, **changes)
        save_remote_state(self.state)
