"""The Remote modal's model: server + tunnel lifecycle, the link, and the switches' autosave.

The modal (``views/remote.py``) is a view over ONE ``RemoteController`` that lives
on the app, so closing and reopening the dialog never restarts a tunnel and the
auto-off timer keeps counting with the dialog closed. Nothing here imports
Textual, so every branch — on/off, ngrok missing, auto-off, restore after a
restart — is a plain unit test.

Persistence (PLAN §1): the two switch values ``remote_enabled`` and
``auto_off_minutes`` sit next to the theme key in ``~/.aisquare/state.json``
through the file's one locked writer (``core.state_file.update_state``), each
key on its own, and in the fleet UI through the autosave the theme uses, off
Textual's thread. Token, password, devices and the write switch are the
SERVER's (``~/.aisquare/remote.json``) and are only read and written through its
API, so ``aisquare remote allow-write`` and ``regenerate-password`` from a shell
and this modal always agree.

Write actions default OFF and this module never flips them on its own: the only
path to ``allow_write=True`` is the user's switch.

Every write of ``remote.json`` the controls ask for (the write switch, the auto-off
timer, a new passphrase, a revoke, a start's deadline) runs on a thread of its own,
one at a time and in the order asked: each waits for ``remote.json.lock``, two
seconds while another process holds it, and on Textual's thread that froze the
fleet UI (sweep of #243). The panel shows what was asked at once, and what landed
once it has. So does the process's first read of the file, which may write it
(:meth:`RemoteController._first_read`).
"""

from __future__ import annotations

import contextlib
import functools
import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import wait as wait_for_futures
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import FrameType, ModuleType
from typing import Any

from aisquare.core.state_file import StateUnwritableError, read_state, update_state
from aisquare.services import remote_server
from aisquare.services.ngrok_tunnel import NgrokTunnel, build_public_url, end_every_tunnel_now

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
SWITCHES = {"remote_enabled": "Remote's on/off switch", "auto_off_minutes": "the auto-off timer"}
"""The two switches ``state.json`` keeps, by key, as a sentence names them."""
PORT_ENV = "AISQUARE_REMOTE_PORT"
"""``serve --port``'s variable, read by ``status`` and ``regenerate-password`` for the link
they print: the panel serves on it too, so one export moves all of them. The panel always
served on 8750, and with the variable exported ``status`` printed a port it was not on."""
TURNING_OFF = "turning Remote off…"
"""The status line while the server and ngrok stop on their own thread (:meth:`turn_off`)."""
STILL_TURNING_OFF = "Remote is still turning off — switch it on again in a moment"
OFF_WAIT_SECONDS = 1.0
"""How long :meth:`RemoteController.turn_on` waits for a Remote still turning off: its server
must be down, its tunnel gone and ``remote.json`` cleared before another starts, or the old
Remote's last steps would undo the new one's. Past that, the switch says to try again rather
than hold Textual's thread for the rest of a slow stop."""
OFF_CLOSES_SECONDS = 2.0
"""How long turning Remote off waits for the phones' sockets to close (4410) before it stops
ngrok, through which alone the close reaches them (:meth:`RemoteController.turn_off`)."""

UNREACHABLE = "Remote is on, but phones cannot reach it"
"""How news of a tunnel that is not up begins (:attr:`RemoteController.on_news`)."""
STARTING = "starting Remote…"
"""The status line while a start's deadline is written, before ngrok starts."""
SAVING = "saving to remote.json…"
"""The status line's line while a write the controls asked for waits (:data:`SAVING_AFTER`)."""
SAVING_AFTER = 0.5
"""How long a write of ``remote.json`` runs before the status line says it is saving: one
takes milliseconds, unless another process holds the file's lock."""
ELSEWHERE_EVERY_SECONDS = 3.0
"""How often the panel looks whether another process serves Remote from this home: a lock
taken for a moment each time (``remote_server.remote_served_elsewhere``)."""
ALREADY_ON = f"Remote could not start — {remote_server.REMOTE_ALREADY_ON}"
"""What a start says that another Remote, on this home, kept off."""

TunnelFactory = Callable[[int], NgrokTunnel]
CallBack = Callable[[Callable[[], None]], object]
"""Runs a step on the thread that drives the controller (:attr:`RemoteController.call_back`)."""


def _call_now(step: Callable[[], None]) -> None:
    step()


def ngrok_without_its_api(port: int) -> NgrokTunnel:
    """The panel's ngrok, for ``port``: its agent API off, which any user of the machine could
    otherwise use to start a tunnel of their own in it, or stop Remote's and start it again
    with the inspector on (``ngrok_tunnel``)."""
    return NgrokTunnel(port, api_off=True)


NewsListener = Callable[[str, bool], None]
"""Told a sentence, and whether it is trouble (``False``: good news), on any thread."""

log = logging.getLogger(__name__)


def _panel_port() -> tuple[int, str | None]:
    """The port :data:`PORT_ENV` names, 8750 when it names none, and why not when it is no port."""
    raw = os.environ.get(PORT_ENV, "").strip()
    if not raw:
        return remote_server.DEFAULT_PORT, None
    try:
        port = int(raw)  # as serve's --port reads it
    except ValueError:
        port = 0
    if not 0 < port < 65536:
        return remote_server.DEFAULT_PORT, f"{PORT_ENV} is {raw!r}, not a port"
    return port, None


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


SwitchSaver = Callable[[str, object], None]
"""Hands one switch's new value to ``state.json``, by key: :func:`update_state`'s shape, which
raises :class:`StateUnwritableError` when the file refuses it."""


class RemoteController:
    """Turn Remote on and off, and answer what the modal paints.

    ``server`` is :mod:`aisquare.services.remote_server`, ``tunnel_factory``
    builds the ngrok subprocess wrapper, ``now`` is the clock — all three are
    seams for the tests. Every time here carries its offset; a naive one from
    ``now`` is read as local time. ``port`` is :data:`PORT_ENV`'s when not given.
    """

    def __init__(
        self,
        *,
        server: ModuleType = remote_server,
        tunnel_factory: TunnelFactory = ngrok_without_its_api,
        dist_dir: Path | None = None,
        port: int | None = None,
        now: Callable[[], datetime] = _utc_now,
        state: RemoteState | None = None,
        url_timeout: float = 15.0,
    ) -> None:
        self._server = server
        self._tunnel_factory = tunnel_factory
        self._dist_dir = dist_dir
        self._port, self._port_problem = (port, None) if port is not None else _panel_port()
        self._now = now
        self._url_timeout = url_timeout
        self.state = state if state is not None else load_remote_state()
        self.info: remote_server.RemoteInfo | None = None
        self.tunnel: NgrokTunnel | None = None
        self.public_url: str | None = None
        """``https://<ngrok-host>/r/<token>`` once the tunnel announced itself."""
        self.message: str | None = None
        """What the status line says: waiting for ngrok, the install hint, an error."""
        self.save_problem: str | None = None
        """The last write of ``remote.json`` a control could not make, until one goes through.

        Every write puts the whole state the server holds, the failed change included, so
        the next one that lands has saved that too, and the sentence goes then. A field
        of its own: kept in :attr:`message`, nothing cleared it but a sentence about
        ngrok, and the panel went on saying the write switch or a revoke had not been
        saved long after both had been (sweep of #243)."""
        self.read_problem: str | None = None
        """Why ``remote.json`` could not be read at the last paint, until it can be: a file that
        is no JSON object, one this account may not read. The panel showed writes off and no
        devices, and said why only once Remote was switched on (sweep of #243)."""
        self.auto_off_at: datetime | None = None
        self._deadline_unsaved = False
        """Set by a write of the deadline that failed, until one goes through or the server is
        seen to read the file again: till then the running server may hold a deadline
        ``remote.json`` does not (:meth:`adopt_server_deadline`)."""
        self._waiter: threading.Thread | None = None
        self._lock = threading.Lock()
        """Around the tunnel, the link and the status line as a tunnel's URL lands on them: a URL
        comes from ngrok's own threads, whenever ngrok announces it (:meth:`_adopt_tunnel_url`),
        and must not land on a Remote turned off, or on a tunnel replaced, meanwhile."""
        self._stopper: threading.Thread | None = None
        """The thread that stops the server and ngrok after :meth:`turn_off`, the latest one."""
        self._stopping_tunnel: NgrokTunnel | None = None
        """The ngrok that thread stops once the server has, for :meth:`stop_tunnel_now`."""
        self.save_switch: SwitchSaver = update_state
        """How a switch that changed reaches ``state.json``: that one key, at once, on the
        caller's thread. The fleet UI hands it to a saver of its own instead, which writes on
        a thread of its own and says a refusal itself, as the theme's does (``FleetApp``)."""
        self._refused_switches: dict[str, str] = {}
        """A switch ``state.json`` refused, by key, as the status line says it, until a later
        save of that switch lands."""
        self.call_back: CallBack = _call_now
        """How a step a write leads to reaches the thread that drives the controller: a start
        saves its switch there, once its deadline is written, so that the save and a turn-off
        meanwhile come in the order they happened. The fleet UI hands it Textual's
        ``call_later``; with nothing else driving the controller, the step runs at once."""
        self.on_done: NewsListener | None = None
        """Told, on the writer's thread, what a control's write did once it has: a new
        passphrase, a device revoked. The panel said so as the button was pressed, which
        was when the write was done; it is done after now (:meth:`_remote_json_write`)."""
        self._writes = ThreadPoolExecutor(max_workers=1, thread_name_prefix="remote-json")
        """``remote.json``'s writes the controls ask for, one at a time and in order: each
        waits for the file's lock, and on Textual's thread that froze the fleet UI. Its
        thread is joined at the interpreter's exit, so a write asked for before a quit
        lands."""
        self._writes_lock = threading.Lock()
        self._reading: Future[None] | None = None
        """The process's first read of ``remote.json`` on the writer's thread, once a paint or a
        restore asked for it (:meth:`_first_read`)."""
        self._reading_lock = threading.Lock()
        self._last_write: Future[None] | None = None
        self._writing_since: float | None = None
        """When the writes in hand began, while any is queued or running."""
        self._writes_queued = 0
        self._deadline_writes = 0
        """Writes queued or running that hand the server a deadline already in hand
        (:attr:`auto_off_at`): till they land, the server's says nothing of the panel's."""
        self._write_switch_writes = 0
        self._write_switch_wanted: bool | None = None
        """The write switch as the last press asked, while its write is queued or running."""
        self.elsewhere = False
        """Whether another process served Remote from this home, the last time it was looked
        for (:meth:`served_elsewhere`)."""
        self._elsewhere_lock = threading.Lock()
        self._elsewhere_at: float | None = None
        self._elsewhere_looking = False
        self.on_news: NewsListener | None = None
        """Told what the human should hear with the R panel closed, on whichever thread
        learned it: a Remote that did not come back at a TUI start, a tunnel that did not
        come up, came up late or came back on a new link, auto-off, and what turning off
        could not do. Only the panel's status line said any of it, and a Remote that failed
        to come back as the human sat down was found out from the phone, away from the
        desk (sweep of #243). The fleet UI toasts it."""
        self._news_lock = threading.Lock()
        self._last_news: str | None = None
        """The last sentence told, so a tunnel failing the same way every minute says it once."""
        self._unreachable_told = False
        """A tunnel not up was told of: the URL that comes after it is good news."""

    # --- on / off -----------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self.info is not None

    def turn_on(self, *, wait: bool = True) -> None:
        """Start the server, then the tunnel; the public URL arrives on a background thread.

        Remote stays on only when ``remote.json`` took its auto-off deadline. A file that
        will not be written (a full disk, a read-only home) cannot sign a phone in either,
        and the ``PermissionError`` raised out of ``restore()`` in ``FleetApp.on_mount``
        ended the fleet UI at start with uvicorn still serving in its thread. The server
        is stopped again instead, which leaves Remote as any start that fails does: off,
        and the status line saying why. A press that fails saves the switch off too
        (:meth:`_start_failed`); a TUI start that fails (:meth:`restore`) keeps it on.

        The server starts on the caller's thread; the deadline's write, the saved switch
        and ngrok follow on the writer's (:meth:`_finish_start`), and ``wait=False``
        returns before them, which is how the fleet UI turns Remote on: the write waited
        for ``remote.json``'s lock on Textual's thread (sweep of #243). ``wait`` waits for
        them, and for the stopping of a start that could not write its deadline.

        A Remote still turning off is waited for, a moment at most (:data:`OFF_WAIT_SECONDS`):
        its last steps clear the deadline and the public origin in the running process,
        and would clear this Remote's.
        """
        self._turn_on(wait=wait, restoring=False)

    def _turn_on(self, *, wait: bool, restoring: bool) -> bool:
        """:meth:`turn_on`; whether the server is up (or was already). ``restoring``: a start of
        the TUI's, whose trouble is news (:meth:`restore`)."""
        if self.running:
            return True
        if not self.wait_until_off(OFF_WAIT_SECONDS):
            self.message = STILL_TURNING_OFF
            return False
        self.public_url = None
        with self._news_lock:  # a new Remote: what the last one said may be news again
            self._last_news, self._unreachable_told = None, False
        if self._port_problem is not None:  # a sentence, never Remote on another port
            self.message = f"Remote could not start — {self._port_problem}"
            if not restoring:
                self._start_failed()
            return False
        try:
            info = self._server.start_remote_server(self._dist_dir, port=self._port)
        except Exception as exc:  # the remote extra is missing, or the port is taken
            # RemoteUnavailable / RemoteError carry the sentence to show; Remote stays
            # off, and a press saves its switch off, so a restart does not retry blindly.
            self.info = None
            self.message = f"Remote could not start — {exc}"
            if not restoring:
                self._start_failed()
            return False
        # The write switch is not touched: it is remote.json's, as the shell left it.
        self.info, self.message = info, STARTING
        deadline = self._hold_deadline()
        start = functools.partial(self._finish_start, info, deadline, restoring)
        written = self._remote_json_write(start, deadline=True)
        if wait:
            wait_for_futures([written])
            self.wait_until_off()  # a start that could not write its deadline stops again
        return True

    def _finish_start(
        self, info: remote_server.RemoteInfo, deadline: datetime | None, restoring: bool
    ) -> None:
        """The rest of a start, on the writer's thread: the deadline to the server, then the
        saved switch (on the thread that drives the controller, :attr:`call_back`) and
        ngrok. Nothing of it for a Remote turned off meanwhile, nor for a Remote whose
        deadline would not write, which is stopped again."""
        if self.info is not info:
            return
        try:
            self._write_deadline(deadline)
        except Exception as exc:  # remote.json will not write: no Remote without its deadline
            # Never raises, and saves no switch: a press saves it off once this is stopped
            # (_start_failed), a TUI start keeps it on. The stopping's own failures are this
            # one again (the deadline cleared in the same file), so the sentence stands alone.
            stopped = self.turn_off(
                persist=False,
                wait=False,
                status=f"Remote could not start — remote.json could not be written: {exc}",
                report=False,
                serving=info,
            )
            if stopped and restoring:
                self._remote_news(self.message)
            elif stopped:
                self.call_back(self._start_failed)
            return
        with self._lock:
            if self.info is not info:
                return
            self.message = None
            self.save_problem = None  # the deadline's write put the whole state
        self.call_back(functools.partial(self._save_started, info))
        tunnel = self._watched_tunnel()
        failure = tunnel.start_tunnel()
        with self._lock:  # its URL may land at once, and must not find "starting" after it
            current = self.info is info
            if current and failure is not None:
                # No tunnel, but the local server is up: the modal keeps the local link
                # (PLAN §6 fallback) and the status line says what to install.
                self.message, self.tunnel = failure, None
            elif current:
                self.tunnel, self.message = tunnel, "starting ngrok…"
        if not current:  # turned off as ngrok started: this one is nobody's to stop
            if failure is None:
                tunnel.stop_tunnel()
            return
        if failure is not None:
            if restoring:
                self._unreachable(failure)
            return
        self._waiter = threading.Thread(
            target=self._await_url, args=(tunnel,), name="ngrok-url", daemon=True
        )
        self._waiter.start()

    def _start_failed(self) -> None:
        """A start the switch asked for failed: the saved switch is off too, as the panel shows
        it, on the thread that drives the controller.

        One that a TUI start could not bring back stayed on, and every start tried again and
        warned again, while the panel's switch read off and a press of it failed before
        any turn-off could save the off: nothing in the UI let a human who no longer wanted
        Remote stop it (sweep 3 of #243). Not while another Remote is on for this home (a
        ``serve``, another fleet UI): what it saved is its own, in the same ``state.json``.
        """
        if self.info is None and self.state.remote_enabled and self.message != ALREADY_ON:
            self._set_state(remote_enabled=False)

    def _save_started(self, info: remote_server.RemoteInfo) -> None:
        """Save that Remote is on, on the thread that drives the controller, where a turn-off
        saves its own off: one that came before this step found the Remote gone."""
        if self.info is info:
            self._set_state(remote_enabled=True)

    def _watched_tunnel(self) -> NgrokTunnel:
        """A tunnel for this Remote's port that hands every URL it announces to
        :meth:`_adopt_tunnel_url`, however late it comes."""
        tunnel = self._tunnel_factory(self._port)
        tunnel.on_announce = functools.partial(self._adopt_tunnel_url, tunnel)
        return tunnel

    def _await_url(self, tunnel: NgrokTunnel) -> None:
        """Wait for the tunnel's URL; past :attr:`_url_timeout`, say why there is none yet.

        The wait ends there, the watching does not: ngrok retries a session it could not
        open, and ``restore()`` brings Remote back at a TUI start, often before a waking
        laptop's Wi-Fi is up. The URL announced a minute later reaches
        :meth:`_adopt_tunnel_url` from the tunnel's log reader. Waited for once, it was
        never shown, nor noted for push links, until Remote was turned off and on (r3
        review of #243).

        The trouble is told under the lock that adoption takes, as it is decided: told
        after letting go, a URL adopted in between found nothing told yet and said
        nothing, and the trouble followed it, for a Remote that had its link by then.
        """
        url = tunnel.wait_for_url(self._url_timeout)
        if url is not None:
            self._adopt_tunnel_url(tunnel, url)
            return
        with self._lock:
            if tunnel is not self.tunnel or self.public_url is not None:
                return
            self.message = tunnel.error or "ngrok did not announce a tunnel in time"
            self._unreachable(self.message)

    def _adopt_tunnel_url(self, tunnel: NgrokTunnel, url: str) -> None:
        """Show ``url`` as the link and note it for push links, while ``tunnel`` is this
        Remote's: one turned off, or replaced by a restart, speaks for no Remote now.

        From the thread that waited for the URL and from the tunnel's log reader, both:
        the first to land shows it, and the same URL again changes nothing. A restarted
        tunnel's says so, and whether the link changed (SPEC §5.8).
        """
        with self._lock:
            info = self.info
            if tunnel is not self.tunnel or info is None:
                return
            link = build_public_url(url, info.token)
            if link == self.public_url:
                return
            self.public_url = link
            moved = tunnel is self._revived_tunnel and link != self._link_before_revive
            if tunnel is self._revived_tunnel:
                changed = "; the link changed" if moved else ""
                self.message = f"ngrok stopped — restarted it{changed}"
            else:
                self.message = None
            # Under the lock, so a Remote turned off meanwhile forgets it after, not before.
            self._note_public_url(link)
        with self._news_lock:
            told, self._unreachable_told, self._last_news = self._unreachable_told, False, None
        if moved:  # every phone's link is dead: the human at the desk has the new one to give
            self._remote_news("ngrok stopped and came back on a new link — R shows it")
        elif told:
            self._remote_news("ngrok is up — phones can reach Remote now", trouble=False)

    def turn_off(
        self,
        *,
        persist: bool = True,
        reason: str = "remote off",
        wait: bool = True,
        status: str | None = None,
        report: bool = True,
        serving: remote_server.RemoteInfo | None = None,
    ) -> bool:
        """Stop the server and the tunnel. ``persist=False`` keeps the saved switch (app exit).
        ``serving``: only the Remote of that server, if it is still the one on (a start's own
        failure, found on the writer's thread); whether it stopped one.

        Turning Remote off (the switch, auto-off) revokes every device after the
        farewell push, so no phone keeps a cookie for a Remote that is off: their
        sockets close with 4410, which the page reads as "Remote is off", and the
        tunnel goes once those closes have reached them, :data:`OFF_CLOSES_SECONDS` at
        most. Leaving the TUI revokes nothing: ``restore()`` brings Remote back at the
        next start, and the devices' own expiry bounds them meanwhile.

        ngrok stops before the server: uvicorn lets go of the port as it begins to stop,
        then waits up to 5 s for a phone's write, and ngrok, stopped after it, forwarded
        the public link to a free ``127.0.0.1:<port>`` all that while. Any account on the
        machine may bind it, and a page's reconnect carried the token and its cookie
        there, a cookie the next Remote takes after a quit (sweep 4 of #243).

        The controller reads off at once, and the stopping runs on a thread of its
        own (``remote-off``); ``wait=False`` returns without waiting for it, which is
        how the fleet UI turns Remote off (the switch, auto-off and quit). Stopping
        waits for uvicorn, whose shutdown waits for a needs scan in flight and the
        push sender, then for ``remote.json``'s lock and for ngrok to exit, and on
        Textual's thread that froze the fleet UI for seconds (r3 review of #243). The
        status line says :data:`TURNING_OFF` meanwhile, or ``status`` when given;
        once the thread is done it says ``status``, then what stopping could not do,
        if anything and ``report`` says to. :meth:`turn_on` waits for the thread, and
        so does :meth:`wait_until_off`.

        Nothing the server raises keeps Remote on. Each step runs whatever the one
        before it raised, the first failure is the status line's sentence, and
        ngrok stops and the controller reads off in any case: auto-off calls this
        from a Textual timer, where an exception ends the whole fleet UI, and a
        ``remote.json`` that would not write once left ngrok up and the switch on.
        """
        with self._lock:  # a URL landing now must find the Remote off (_adopt_tunnel_url)
            if serving is not None and self.info is not serving:
                return False  # turned off, or on again, since: that one is not this to stop
            served, tunnel = self.info is not None, self.tunnel
            self.info = None
            self.tunnel = None
            self.public_url = None
            self.auto_off_at = None
        if persist:
            self._set_state(remote_enabled=False)
        if not served and tunnel is None:
            self.message = status
            return False
        self.message = status or TURNING_OFF
        stopper = threading.Thread(
            target=self._stop_remote,
            args=(served, tunnel, persist, reason, status, report),
            name="remote-off",
            # Not a daemon: the interpreter waits for it at exit, so no ngrok outlives the
            # TUI whatever quit did not wait for.
            daemon=False,
        )
        self._stopper, self._stopping_tunnel = stopper, tunnel
        stopper.start()
        if wait:
            stopper.join()
        return True

    def _stop_remote(
        self,
        served: bool,
        tunnel: NgrokTunnel | None,
        persist: bool,
        reason: str,
        status: str | None,
        report: bool,
    ) -> None:
        """:meth:`turn_off`'s stopping, on its own thread; the status line says how it ended.

        The writes the controls asked for before it land first (:meth:`_remote_json_write`): a
        start's deadline, written after this cleared it, would hold ``remote.json`` past
        Remote. ngrok stops before the server, which holds the port until then."""
        failure: str | None = None
        self.writes_done()
        try:
            if served:
                failure = self._revoke_and_clear(persist, reason)
        finally:
            if tunnel is not None:
                try:
                    tunnel.stop_tunnel()
                except Exception as exc:  # a thread of its own: nobody else would hear of it
                    failure = failure or f"Remote is off, but ngrok did not stop cleanly — {exc}"
            if served:
                try:
                    self._server.stop_remote_server()
                except Exception as exc:
                    failure = failure or f"Remote is off, but stopping its server failed — {exc}"
            if status is None:
                self.message = failure
            elif failure is None or not report:
                self.message = status
            else:
                # What turning off could not do still shows: a revoke that failed leaves
                # phones holding cookies the next Remote accepts.
                self.message = f"{status}. {failure}"
            if report:
                self._remote_news(failure)

    def _revoke_and_clear(self, persist: bool, reason: str) -> str | None:
        """What turning off does while the server still holds its port: every device revoked
        (``persist``), the public origin and the deadline cleared, and the revoked sockets'
        closes waited for. What it could not do, as the status line says it; ``None`` when
        it did all of it."""
        failure: str | None = None
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
        else:
            self.save_problem = None  # that write put the whole state
        if persist:
            try:  # the 4410s reach the phones through ngrok alone, which stops next
                self._server.remote_wait_for_closes(OFF_CLOSES_SECONDS)
            except Exception:  # a close that never lands costs its phone the reason, no more
                log.warning("remote: the phones' closes could not be waited for", exc_info=True)
        return failure

    def wait_until_off(self, timeout: float | None = None) -> bool:
        """Wait for the server and ngrok of a Remote turned off to stop; whether they had.

        ``timeout`` bounds the wait, ``None`` waits as long as stopping takes (each of its
        steps is bounded). ``run_ui`` waits here once the terminal is back, so the
        process never ends before its ngrok.
        """
        stopper = self._stopper
        if stopper is None or not stopper.is_alive():
            return True
        stopper.join(timeout)
        return not stopper.is_alive()

    def stop_tunnel_now(self) -> None:
        """Stop the ngrok of a Remote still turning off, on the calling thread, server or not.

        For a process about to end at once (a Ctrl-C while ``run_ui`` waits here): the
        thread :meth:`turn_off` left stopping stops ngrok only once the phones' sockets have
        closed and ``remote.json`` is written, and it ends with the process, so the ngrok it
        had not reached yet ran on, its tunnel still up. One that thread stopped already is
        stopped again for nothing.
        """
        tunnel = self._stopping_tunnel
        if tunnel is not None:
            tunnel.stop_tunnel()

    def restore(self, *, wait: bool = True) -> None:
        """At TUI start: a Remote that was on when the TUI last exited comes back on.

        What kept it off, or kept ngrok from starting, is news (:attr:`on_news`): this runs
        as the human sits down, often just before leaving the desk with the phone. Another
        process serving this home is no news: Remote is on, which is what the switch was
        saved for, and the panel says where. "Could not start — turn it off first", at every
        start of a second fleet UI, pointed at the Remote that worked (sweep 3 of #243).

        ``remote.json`` is read first, on the writer's thread (:meth:`_first_read`), and
        with ``wait=False`` the start follows on the thread that drives the controller
        (:attr:`call_back`) once it has been, read or not: a file that could not be read is
        the start's to say.
        """
        if not self.state.remote_enabled or self.running:
            return
        reading = self._first_read()
        if reading is not None and not wait:
            reading.add_done_callback(lambda _read: self.call_back(self._restore_read))
            return
        if reading is not None:
            wait_for_futures([reading])
        self._restore_read(wait=wait)

    def _restore_read(self, *, wait: bool = False) -> None:
        """:meth:`restore`'s start, ``remote.json`` read; nothing for a Remote turned on, or its
        switch turned off, meanwhile."""
        if not self.state.remote_enabled or self.running:
            return
        if self._turn_on(wait=wait, restoring=True):
            return  # what came after the server is said as it ends
        if self.message == ALREADY_ON:
            self.message = None  # on elsewhere: the panel's state says so (served_elsewhere)
            return
        self._remote_news(self.message)

    def shutdown_for_exit(self, *, wait: bool = True) -> None:
        """At TUI exit: end the processes, keep the saved switches for ``restore``.

        ``wait=False`` leaves them stopping on their own thread (:meth:`turn_off`);
        :meth:`wait_until_off` is then where the exit waits for them.

        A Remote still starting is saved as on first: a start saves its switch once its
        deadline is written (:meth:`_save_started`), on the thread that drives the
        controller, and a fleet UI that quit before then took no more steps, so a Remote
        turned on and left at once did not come back at the next start.

        A Remote whose auto-off time has come is turned off as auto-off turns it off, its
        phones signed out and its switch saved off. The check runs every 30 s, and a quit
        in between left the deadline to the exit's stopping: it cleared it before the
        server stopped, which let unlocks in again past it, and revoked nothing, so every
        phone stayed signed in on the Remote the next start brought back (review of #243,
        round 5).
        """
        if self.enforce_auto_off(wait=wait):
            return
        with self._lock:
            starting = self.info is not None and not self.state.remote_enabled
        if starting:
            self._set_state(remote_enabled=True)
        self.turn_off(persist=False, wait=wait)

    # --- the controls ---------------------------------------------------------------------

    def write_actions_allowed(self) -> bool:
        """The write switch as ``remote.json`` holds it, Remote on or off; off when unreadable.
        While a press of it is still being written, what that press asked."""
        wanted = self._write_switch_wanted
        if wanted is not None:
            return wanted
        if self._first_read() is not None:
            return False
        try:
            return bool(self._server.remote_allow_write())
        except Exception:  # a remote.json that cannot be read shows writes as off
            return False

    def set_allow_write(self, enabled: bool, *, wait: bool = True) -> None:
        """Flip the one write switch, in ``remote.json``, whether Remote is on or not: the
        TUI's next Remote and ``asq remote serve`` both start with what it says.

        A switch the file will not take still holds in this TUI until its server reads the
        file again: a write that fails has already changed the server's state in memory,
        and nothing rolls it back. So the status line says it was not saved; "could not be
        changed" sat beside a switch that showed the change, which phones had too.
        ``wait=False`` returns before the write (:meth:`_remote_json_write`).
        """
        enabled = bool(enabled)
        with self._writes_lock:
            self._write_switch_wanted = enabled
            self._write_switch_writes += 1

        def remote_json_job() -> None:
            try:
                self._server.set_allow_write(enabled)
            except Exception as exc:  # an unwritable remote.json is a sentence, not a crash
                self.save_problem = f"write actions could not be saved to remote.json — {exc}"
            else:
                self.save_problem = None
            finally:
                with self._writes_lock:
                    self._write_switch_writes -= 1
                    if not self._write_switch_writes:
                        self._write_switch_wanted = None

        self._then(self._remote_json_write(remote_json_job), wait)

    def set_auto_off(self, minutes: int | None, *, wait: bool = True) -> None:
        """Pick a timer, or ``None`` for Never. Takes effect at once while Remote is on.

        A timer ``remote.json`` will not take still takes effect, and the status line says
        it was not saved: raised into the Auto-off picker's handler, the error ended the
        fleet UI. The running server has the timer too, since a write that fails has
        already changed its state in memory and nothing rolls that back, so the panel and
        the server's gate end Remote at the same time until the server reads the file
        again (:meth:`adopt_server_deadline`). ``wait=False`` returns before the write.
        """
        if minutes not in AUTO_OFF_CHOICES:
            raise ValueError(f"auto-off must be one of {AUTO_OFF_CHOICES}, not {minutes}")
        self._set_state(auto_off_minutes=minutes)
        if not self.running:
            return
        deadline = self._hold_deadline()

        def remote_json_job() -> None:
            try:
                self._write_deadline(deadline)
            except Exception as exc:  # an unwritable remote.json is a sentence, not a crash
                self.save_problem = f"auto-off could not be saved to remote.json — {exc}"
            else:
                self.save_problem = None

        self._then(self._remote_json_write(remote_json_job, deadline=True), wait)

    def regenerate_password(self, *, wait: bool = True) -> str | None:
        """A new passphrase from the server; ``None`` while Remote is off (nothing to unlock),
        or when ``remote.json`` would not take it, which the status line then says. The
        running server has the new one all the same, and has signed every phone out: the
        panel shows it until the server reads the file again after another process rewrote
        it, which brings back the old passphrase, and the devices with it.

        ``wait=False`` returns ``None`` before the write, and :attr:`on_done` is told once
        the new passphrase is in."""
        if self.info is None:
            return None
        made: list[str] = []

        def remote_json_job() -> None:
            try:
                password = str(self._server.regenerate_password())
            except Exception as exc:  # raised into the Regenerate button's handler: the UI ended
                self.save_problem = f"the new password could not be saved to remote.json — {exc}"
                return
            self.save_problem = None
            made.append(password)
            self._done("New password — every device has to unlock again")

        written = self._remote_json_write(remote_json_job)
        if not wait:
            return None
        wait_for_futures([written])
        return made[0] if made else None

    def remote_status(self) -> dict[str, Any]:
        """``remote_server_status()``; ``{}`` while ``remote.json`` cannot be read, which the
        status line then says (:attr:`read_problem`), or is being read for the first time
        (:meth:`_first_read`). A paint reads it once and hands it to :meth:`devices` and
        :meth:`unlock_failures`."""
        if self._first_read() is not None:
            return {}
        try:
            status = self._server.remote_server_status()
        except Exception as exc:  # the file costs the list and the switches, not the modal
            self.read_problem = f"remote.json could not be read — {exc}"
            return {}
        self.read_problem = None
        return status if isinstance(status, dict) else {}

    def devices(self, status: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """The devices ``[{id, ua, first_seen, last_seen, expires_at, signed_in}]``, by id,
        from ``status`` (:meth:`remote_status`), read now when none is given."""
        rows = (self.remote_status() if status is None else status).get("devices", [])
        rows = rows if isinstance(rows, list) else []
        return [row for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)]

    def revoke_device(self, device_id: str, *, wait: bool = True) -> bool:
        """Revoke one device; ``False`` when ``remote.json`` would not take it, which the
        status line then says (raised into the Revoke button's handler, it ended the UI).
        The running server has signed it out all the same, until it reads the file again
        after another process rewrote it.

        ``wait=False`` returns ``False`` before the write, and :attr:`on_done` is told once
        the device is revoked."""
        revoked: list[bool] = []

        def remote_json_job() -> None:
            try:
                self._server.revoke_remote_device(device_id)
            except Exception as exc:  # an unwritable remote.json is a sentence, not a crash
                self.save_problem = f"{device_id} could not be revoked in remote.json — {exc}"
                return
            self.save_problem = None
            revoked.append(True)
            self._done(f"Revoked {device_id}")

        written = self._remote_json_write(remote_json_job)
        if not wait:
            return False
        wait_for_futures([written])
        return bool(revoked)

    # --- the writes of remote.json ------------------------------------------------------------

    def _first_read(self) -> Future[None] | None:
        """``None`` once this process has read ``remote.json``, so that reading it only reads;
        until then the read on the writer's thread, asked for now unless one is in flight.

        The process's first read makes the file when it is missing and rewrites one that is
        old or edited by hand, under ``remote.json.lock`` (``remote_server.runtime``): the R
        panel's first paint did it on Textual's thread, and so did a Remote restored at the
        UI's start, which froze for two seconds while another process held that lock (sweep
        3 of #243). Meanwhile the panel paints what it paints for a file it cannot read: writes
        off, no devices, no passphrase. A read that failed is asked for again at the next
        paint, and why it failed is the status line's (:attr:`read_problem`).
        """
        if self._server.remote_state_loaded():
            return None
        with self._reading_lock:
            reading = self._reading
            if reading is None or reading.done():
                reading = self._reading = self._remote_json_write(self._read_remote_json)
            return reading

    def _read_remote_json(self) -> None:
        """The first read of ``remote.json``, on the writer's thread (:meth:`_first_read`)."""
        try:
            self._server.runtime()
        except Exception as exc:  # the paints say why, as a read of theirs did
            self.read_problem = f"remote.json could not be read — {exc}"
        else:
            self.read_problem = None

    def _remote_json_write(
        self, job: Callable[[], None], *, deadline: bool = False
    ) -> Future[None]:
        """Hand ``job``, a write of ``remote.json`` and what follows it, to the writer's thread,
        after every write asked for before it. ``deadline``: it hands the server the deadline
        in hand (:meth:`adopt_server_deadline` waits for it)."""

        def remote_json_run() -> None:
            try:
                job()
            except Exception:  # a write's own sentence is on the status line already
                log.warning("remote: a write of remote.json failed", exc_info=True)
            finally:
                with self._writes_lock:
                    self._writes_queued -= 1
                    if deadline:
                        self._deadline_writes -= 1
                    if not self._writes_queued:
                        self._writing_since = None

        with self._writes_lock:
            if not self._writes_queued:
                self._writing_since = time.monotonic()
            self._writes_queued += 1
            if deadline:
                self._deadline_writes += 1
            written = self._writes.submit(remote_json_run)
            self._last_write = written
        return written

    def writes_done(self, timeout: float | None = None) -> bool:
        """Wait for every write asked for so far; whether they were all done in ``timeout``."""
        last = self._last_write
        if last is None:
            return True
        done, _left = wait_for_futures([last], timeout)
        return bool(done)

    @staticmethod
    def _then(written: Future[None], wait: bool) -> None:
        if wait:
            wait_for_futures([written])

    def _done(self, news: str) -> None:
        """Tell :attr:`on_done` what a write did, on the writer's thread."""
        hear = self.on_done
        if hear is None:
            return
        try:
            hear(news, False)
        except Exception:  # on the writer's thread: nobody else would hear of it
            log.warning("remote: what a write did could not be told: %s", news, exc_info=True)

    def unlock_failures(self, status: dict[str, Any] | None = None) -> tuple[int, str | None]:
        """Wrong passphrases in the last 30 min, and until when new unlocks are paused, from
        ``status`` (:meth:`remote_status`), read now when none is given."""
        status = self.remote_status() if status is None else status
        failed, until = status.get("failed_unlocks"), status.get("locked_out_until")
        return (
            failed if isinstance(failed, int) else 0,
            until if isinstance(until, str) and until else None,
        )

    # --- what the modal paints ---------------------------------------------------------------

    def status_line(self) -> str:
        """The status line: what Remote is doing or why it is not, then what ngrok's agent API
        allows while it is on (:attr:`NgrokTunnel.api_warning`), :data:`SAVING` while a write
        of ``remote.json`` waits, one that did not land (:attr:`save_problem`), why the file
        could not be read (:attr:`read_problem`) and a switch ``state.json`` refused, each on
        a line of its own."""
        tunnel = self.tunnel
        api = tunnel.api_warning if tunnel is not None else None
        since = self._writing_since
        saving = SAVING if since is not None and time.monotonic() - since >= SAVING_AFTER else None
        lines = (
            self.message,
            api,
            saving,
            self.save_problem,
            self.read_problem,
            *self._refused_switches.values(),
        )
        return "\n".join(line for line in lines if line)

    def served_elsewhere(self) -> bool:
        """Whether another process serves Remote from this home (``asq remote serve``, another
        fleet UI), as last found; ``False`` while this one serves.

        The panel said "off" while one did, publicly tunnelled, its phones listed as signed in
        in the same panel and its write switch flipped from it (sweep of #243). Looked for at
        most every :data:`ELSEWHERE_EVERY_SECONDS`, on a thread of its own: a lock call on
        NFS can block however non-blocking it is. Once the home is found free, a start's
        sentence that another Remote kept it off goes.
        """
        if self.running:
            self.elsewhere = False  # whatever a look made as this one claimed the home found
            return False
        now = time.monotonic()
        with self._elsewhere_lock:
            fresh = self._elsewhere_at is not None and (
                now - self._elsewhere_at < ELSEWHERE_EVERY_SECONDS
            )
            if self._elsewhere_looking or fresh:
                return self.elsewhere
            self._elsewhere_looking, self._elsewhere_at = True, now
        threading.Thread(target=self._look_elsewhere, name="remote-elsewhere", daemon=True).start()
        return self.elsewhere

    def _look_elsewhere(self) -> None:
        try:
            found = bool(self._server.remote_served_elsewhere())
        except Exception:  # a home that cannot tell says nothing of another Remote
            found = False
        self.elsewhere = found and not self.running  # this one's own claim, as it started
        if not found:
            with self._lock:
                if self.message == ALREADY_ON:
                    self.message = None
        with self._elsewhere_lock:
            self._elsewhere_looking = False

    def served_auto_off(self) -> tuple[bool, datetime | None]:
        """Whether ``remote.json`` could be read, and when the Remote serving this home turns
        itself off as it says now (``None``: Never): for the panel while another process
        serves it (:attr:`elsewhere`), whose timer is that process's own, not this UI's."""
        if self._first_read() is not None:
            return False, None
        try:
            return True, self._server.remote_auto_off_at()
        except Exception:  # unreadable for a moment: the panel says nothing of a timer
            return False, None

    def link_url(self) -> str | None:
        """The public link when ngrok is up, else the local one — ``None`` while Remote is off."""
        if self.info is None:
            return None
        return self.public_url or self.info.url_local

    def password(self) -> str | None:
        """The passphrase as ``remote.json`` says now: a ``regenerate-password`` from a shell
        shows at the next paint, not the one this Remote started with. ``None`` while off,
        unless another process serves this home (:attr:`elsewhere`): it is that Remote's."""
        info = self.info
        if info is None and not self.elsewhere:
            return None
        if self._first_read() is not None:
            return None if info is None else info.password
        try:
            return str(self._server.remote_password())
        except Exception:  # unreadable for a moment: the one in hand beats a blank
            return None if info is None else info.password

    # --- auto-off -------------------------------------------------------------------------------

    def _hold_deadline(self) -> datetime | None:
        """Set (or clear, for Never) the deadline in hand, as the panel shows it at once; the
        server has it once :meth:`_write_deadline` has run.

        An instant with its offset, from an aware clock: naive local time plus an
        hour ran an hour long across a DST fall-back, and was published without an
        offset a phone elsewhere read in its own timezone. To the second, as the server
        keeps it: with the clock's microseconds the server's copy read as an EARLIER
        deadline than the panel's own, which :meth:`adopt_server_deadline` took for the
        file's coming back after a write that failed.
        """
        minutes = self.state.auto_off_minutes
        now = _aware(self._now()).replace(microsecond=0)
        self.auto_off_at = None if minutes is None else now + timedelta(minutes=minutes)
        return self.auto_off_at

    def _write_deadline(self, deadline: datetime | None) -> None:
        """Hand the server ``deadline``, which it reports as ``auto_off_at``; raises what the
        write of ``remote.json`` raised. On the writer's thread."""
        # The server shows it as GET /api/remote's auto_off_at (PLAN §4-B); Never is
        # null there, which is the same thing it shows while Remote is off.
        self._deadline_unsaved = True
        self._server.set_auto_off(deadline)
        self._deadline_unsaved = False

    def adopt_server_deadline(self) -> datetime | None:
        """The deadline, moved to the server's when that is later: a phone extended it.

        Called by :meth:`enforce_auto_off` and on every paint, so the extension holds
        and the modal shows the new time. The server enforces it either way.

        A timer ``remote.json`` would not take is the running server's too: a write that
        fails has already changed the server's state in memory, and nothing rolls it back,
        so its gate and the panel end Remote at the same time. But the file keeps the
        deadline the write failed to replace, and the server goes back to it when it reads
        the file again after another process rewrote it (``asq remote allow-write`` in a
        shell, say); its gate ends Remote there from then on (SPEC §2.5). So while the
        deadline in hand is unsaved, an EARLIER deadline of the server's is taken too, and
        so is any in place of Never: keeping the longer timer, or Never, showed Remote on
        while every phone was answered as if it were off. A later one is taken as ever,
        whether a phone extended the unsaved timer or the file held a longer one: the gate
        waits for it.
        """
        if not self.running or (self.auto_off_at is None and not self._deadline_unsaved):
            return self.auto_off_at
        if self._deadline_writes:  # the server has not been handed the deadline in hand yet
            return self.auto_off_at
        try:
            served = self._server.remote_auto_off_at()
        except Exception:  # unreadable for a moment: the deadline in hand still stands
            served = None
        if served is not None:
            served, held = _aware(served), self.auto_off_at
            if self._deadline_unsaved and (held is None or served < held):
                # the server read the file again: the deadline in hand is the file's now
                self.auto_off_at, self._deadline_unsaved = served, False
            elif held is not None and served > held:
                self.auto_off_at = served
        return self.auto_off_at

    def enforce_auto_off(self, *, wait: bool = True) -> bool:
        """Turn Remote off when its timer has run out; ``True`` when it just did.

        ``wait`` is :meth:`turn_off`'s: the app's 30 s timer passes ``False``, so the
        fleet UI never waits for the stopping.
        """
        deadline = self.adopt_server_deadline()
        if self.running and deadline is not None and _aware(self._now()) >= deadline:
            ran_out = "Remote turned off — the auto-off timer ran out"
            self._remote_news(ran_out)  # before what the stopping could not do, if anything
            self.turn_off(reason="auto-off", wait=wait, status=ran_out)
            return True
        return False

    # --- persistence -----------------------------------------------------------------------------

    def _set_state(self, **changes: Any) -> None:
        """Take ``changes`` in hand, and save each switch that changed, on its own.

        Only those keys: the whole snapshot this TUI read at its start went out with every
        save, so a second ``asq ui`` that picked an auto-off wrote back the Remote it had read
        as on, after the first had turned it off, and the next start brought the public
        tunnel back against that off (sweep of #243). A save ``state.json`` refused was
        dropped without a word; it is on the status line now, until one of that switch lands.
        """
        self.state = replace(self.state, **changes)
        for key, value in changes.items():
            stored = NEVER if key == "auto_off_minutes" and value is None else value
            try:
                self.save_switch(key, stored)
            except StateUnwritableError as exc:
                self._refused_switches[key] = f"{SWITCHES[key]} could not be saved — {exc}"
            else:
                self._refused_switches.pop(key, None)

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
        tunnel = self._watched_tunnel()
        failure = tunnel.start_tunnel()
        if failure is not None:  # the dead one stays, so the next minute tries again
            self.message = failure
            self._unreachable(failure)
            return False
        # On a thread of its own, as this runs on Textual's: what the dead ngrok left in its
        # group (a launcher's ngrok, still up) is given its seconds to end.
        threading.Thread(target=dead.stop_tunnel, name="ngrok-stop", daemon=True).start()
        with self._lock:
            if self.public_url is not None:  # else the last restart never came up: keep it
                self._link_before_revive = self.public_url
            self.tunnel = self._revived_tunnel = tunnel
            self.public_url = None  # the modal shows the local link until ngrok announces one
            self.message = "ngrok stopped — restarting it…"
        # Its URL says ngrok was restarted, and whether the link changed (_adopt_tunnel_url);
        # a restart that dies before it announces leaves ngrok's own error on the status line.
        self._waiter = threading.Thread(
            target=self._await_url, args=(tunnel,), name="ngrok-url", daemon=True
        )
        self._waiter.start()
        return True

    def _unreachable(self, why: str | None) -> None:
        """Tell that phones cannot reach this Remote, and ``why``: no tunnel is up."""
        with self._news_lock:
            self._unreachable_told = True
        self._remote_news(f"{UNREACHABLE} — {why}" if why else UNREACHABLE)

    def _remote_news(self, news: str | None, *, trouble: bool = True) -> None:
        """Hand ``news`` to :attr:`on_news`, once for a run of the same sentence."""
        hear = self.on_news
        if not news or hear is None:
            return
        with self._news_lock:
            if news == self._last_news:
                return
            self._last_news = news
        try:
            hear(news, trouble)
        except Exception:  # on ngrok's or the stopper's thread: nobody else would hear of it
            log.warning("remote: news could not be told: %s", news, exc_info=True)

    def _note_public_url(self, url: str) -> None:
        """Tell the server where phones reach it, so push links lead there (SPEC §5.8).

        Only an https URL on a DNS name is an origin: a tunnel that announces
        anything else leaves push links without one, rather than raising in the
        thread that took it (the one that waited for it, or ngrok's log reader).
        """
        with contextlib.suppress(ValueError):
            self._server.note_public_url(url)


ENDING_SIGNALS = ("SIGHUP", "SIGTERM")
"""What ends the fleet UI from outside, by default: a terminal closed under it, a ``kill``."""


@contextlib.contextmanager
def ngrok_ends_with() -> Iterator[None]:
    """For as long as the fleet UI runs: a hangup or a SIGTERM that ends the process ends every
    ngrok it started first (``ngrok_tunnel.end_every_tunnel_now``).

    ngrok runs in a process group of its own, so that stopping it stops what a launcher
    started as well (``ngrok_tunnel``), and the hangup of a terminal closed under the
    fleet UI reaches the UI's group, not ngrok's: the UI died of it, as ever, and its
    ngrok ran on, its tunnel up and its static domain held, so the next start's ngrok
    could not have it (ERR_NGROK_334). Only where the signal ends the process anyway (its
    default action): one ignored, as under ``nohup``, or handled by someone else, is
    left as it is. Signal handlers are the main thread's; elsewhere, and on Windows,
    this does nothing.
    """
    if sys.platform == "win32" or threading.current_thread() is not threading.main_thread():
        yield
        return

    def end(signum: int, frame: FrameType | None) -> None:
        end_every_tunnel_now()
        signal.signal(signum, signal.SIG_DFL)  # and the signal does what it always did
        os.kill(os.getpid(), signum)

    installed: list[signal.Signals] = []
    for name in ENDING_SIGNALS:
        signum = signal.Signals[name]
        if signal.getsignal(signum) is signal.SIG_DFL:
            signal.signal(signum, end)
            installed.append(signum)
    try:
        yield
    finally:
        for signum in installed:
            signal.signal(signum, signal.SIG_DFL)
