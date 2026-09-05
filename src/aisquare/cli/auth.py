"""Signing in from the terminal: ``login``, ``logout``, ``whoami`` and ``aisquare auth``.

The mechanism is the OAuth 2.0 device grant (RFC 8628) against the AISquare
identity provider: the terminal shows a short code and a link, the person
approves in a browser, and this process polls until the server hands over a
token. What it looks like, why it polls rather than listens, and every error
message below are specified in ``docs/plans/aisquare-login.md``.

Output discipline: under ``--json`` stdout carries exactly one object and every
progress line goes to stderr, so ``aisquare --json login | jq`` works.
"""

from __future__ import annotations

import json
import os
import random
import sys
import threading
import time
from collections.abc import Callable
from typing import Annotated
from urllib.parse import urlparse

import typer
from rich.console import Console, Group
from rich.live import Live
from rich.text import Text

from aisquare.cli.common import fail
from aisquare.core import browser
from aisquare.core.console import stderr_console, stdout_console
from aisquare.core.state import get_state
from aisquare.services import auth as auth_service
from aisquare.services import iam

app = typer.Typer(help="Sign in to AISquare and inspect the session.", no_args_is_help=True)

#: Patched by tests so a poll loop runs in milliseconds, and so a deadline can be reached.
_sleep: Callable[[float], None] = time.sleep
_monotonic: Callable[[], float] = time.monotonic

_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_TICK_SECONDS = 0.25
_MAX_BACKOFF_SECONDS = 60.0
_MAX_RETRY_AFTER_SECONDS = 30

ApiUrl = Annotated[
    str | None,
    typer.Option(
        "--api-url", help="API base URL. Default: api_url from config.toml.", metavar="URL"
    ),
]
NoBrowser = Annotated[
    bool, typer.Option("--no-browser", help="Print the link only; never try to open a browser.")
]
WithToken = Annotated[
    bool,
    typer.Option(
        "--with-token",
        help="Read an access token from stdin instead of signing in through the browser.",
    ),
]
LiveFlag = Annotated[
    bool, typer.Option("--live", help="Also ask the server whether the session works.")
]


# --------------------------------------------------------------------------- output helpers


def _progress_console() -> Console:
    """Where progress text goes: stderr under --json, stdout otherwise."""
    return stderr_console() if get_state().json_output else stdout_console()


def _say(message: str) -> None:
    if get_state().quiet and not get_state().json_output:
        return
    _progress_console().print(message)


def _fail(exc: iam.IamError) -> None:
    fail(exc.message, error=exc.code, detail=exc.detail)


def _host(url: str) -> str:
    return urlparse(url).netloc or url


def _interactive() -> bool:
    state = get_state()
    return not state.json_output and not state.quiet and sys.stdout.isatty()


# --------------------------------------------------------------------------- login


def login(
    api_url: ApiUrl = None, no_browser: NoBrowser = False, with_token: WithToken = False
) -> None:
    """Sign in to AISquare through your browser."""
    try:
        resolved = iam.resolve_api_url(api_url)
        if with_token:
            session = auth_service.sign_in_with_token(resolved, sys.stdin.read().strip())
            _emit_signed_in(session)
            return
        if iam.env_session(resolved) is not None:
            fail(
                f"{iam.TOKEN_ENV_VAR} is set, so aisquare is using that token. "
                "Unset it to sign in with the browser.",
                error="env_token_set",
            )
        previous = iam.stored_session()
        if previous is not None:
            _say(
                f"  Already signed in as {previous.email or previous.sub} on "
                f"{_host(previous.api_url)}. Continuing replaces that session."
            )
        endpoints = iam.discover(resolved)
        grant = iam.start_device_authorization(endpoints)
        _show_verification(grant, no_browser=no_browser)
        token = _wait_for_approval(endpoints, grant)
        session = auth_service.complete_sign_in(resolved, endpoints, token)
    except iam.IamError as exc:
        _fail(exc)
    except KeyboardInterrupt:
        fail("Sign-in cancelled. Nothing was stored.", error="cancelled", exit_code=130)
    _emit_signed_in(session)


def _show_verification(grant: iam.DeviceAuthorization, *, no_browser: bool) -> None:
    minutes = max(1, grant.expires_in // 60)
    if get_state().json_output:
        event = {
            "event": "verification",
            "verification_uri_complete": grant.verification_uri_complete,
            "verification_uri": grant.verification_uri,
            "user_code": grant.user_code,
            "expires_in": grant.expires_in,
        }
        typer.echo(json.dumps(event, separators=(",", ":")), err=True)
        _say("If you are an agent, ask the user to visit the URL above.")
    else:
        _say(f"! First, note your one-time code: {grant.user_code}")
        _say("  Check that the browser shows the same code before you authorize.")
        _say("  Open this URL to continue in your browser:")
        _say(f"  {grant.verification_uri_complete}")
        _say(f"  You have {minutes} minutes to approve this request.")
        _say("")

    if no_browser or get_state().json_output:
        _say("  Visit the URL above on any device.")
        return
    if browser.open_url(grant.verification_uri_complete):
        _say(f"  Opening your browser at {_host(grant.verification_uri_complete)}...")
    elif browser.is_headless() or os.environ.get("BROWSER", "x").strip() in browser.PRINT_ONLY:
        _say("  Couldn't open a browser here. Visit the URL above on any device.")
    else:
        _say("⚠ Failed to open a browser. Visit the URL above manually.")


def _wait_for_approval(
    endpoints: iam.Endpoints, grant: iam.DeviceAuthorization
) -> dict[str, object]:
    """RFC 8628 section 3.4 polling, with a countdown and Esc to cancel."""
    deadline = _monotonic() + grant.expires_in + 60
    interval = float(max(1, grant.interval))
    backoff: float | None = None
    cancel = threading.Event()
    status = _LiveStatus(deadline) if _interactive() else None
    watcher = _EscapeWatcher(cancel) if _interactive() and sys.stdin.isatty() else None
    if status is None:
        _say("  Waiting for approval in the browser... Press Ctrl-C to cancel.")
    else:
        watcher_hint = "Esc or Ctrl-C" if watcher else "Ctrl-C"
        status.hint = f"  Press {watcher_hint} to cancel"
    try:
        if watcher:
            watcher.start()
        if status:
            status.start()
        while True:
            wait = backoff if backoff is not None else interval * 1.2 + random.uniform(0, 1)
            if not _pause(wait, cancel, status):
                fail("Sign-in cancelled. Nothing was stored.", error="cancelled", exit_code=130)
            if _monotonic() > deadline:
                fail(
                    "The code expired before it was approved. Run aisquare login again.",
                    error="expired",
                )
            try:
                outcome = iam.poll_token(endpoints, grant.device_code)
            except iam.IamError as exc:
                if exc.code != "unreachable":
                    raise
                backoff = min(_MAX_BACKOFF_SECONDS, (backoff or interval) * 2)
                continue
            backoff = None
            if outcome.kind == "token" and outcome.token is not None:
                return dict(outcome.token)
            if outcome.kind == "pending":
                continue
            if outcome.kind == "slow_down":
                interval = float(outcome.interval or interval + 5)
                continue
            if outcome.kind == "rate_limited":
                backoff = float(min(outcome.retry_after or 30, _MAX_RETRY_AFTER_SECONDS))
                continue
            if outcome.kind == "denied":
                fail(
                    "The request was denied in the browser. Nothing was stored.",
                    error="access_denied",
                )
            if outcome.kind == "expired":
                fail(
                    "The code expired before it was approved. Run aisquare login again.",
                    error="expired",
                )
            if outcome.kind == "paused":
                fail("CLI sign-in is temporarily paused. Try again later.", error="paused")
            raise iam.IamError(
                "unsupported_server", f"Unexpected answer from {endpoints.issuer} while waiting."
            )
    finally:
        if status:
            status.stop()
        if watcher:
            watcher.stop()


def _pause(seconds: float, cancel: threading.Event, status: _LiveStatus | None) -> bool:
    """Sleep in short ticks so a cancel or a countdown update is never far away."""
    remaining = seconds
    while remaining > 0:
        if cancel.is_set():
            return False
        if status:
            status.update(next_check_in=remaining)
        step = min(_TICK_SECONDS, remaining)
        _sleep(step)
        remaining -= step
    return not cancel.is_set()


class _LiveStatus:
    """The single refreshing line while the terminal waits."""

    def __init__(self, deadline: float) -> None:
        self._deadline = deadline
        self._live = Live(Text(""), console=stdout_console(), refresh_per_second=8, transient=True)
        self._frame = 0
        self.hint = "  Press Ctrl-C to cancel"

    def start(self) -> None:
        self._live.start()

    def update(self, *, next_check_in: float) -> None:
        self._frame = (self._frame + 1) % len(_SPINNER)
        left = max(0, int(self._deadline - 60 - _monotonic()))
        line = (
            f"{_SPINNER[self._frame]} Waiting for approval in the browser · "
            f"next check in {int(next_check_in) + 1}s · "
            f"code expires in {left // 60}:{left % 60:02d}"
        )
        self._live.update(Group(Text(line), Text(self.hint, style="dim")))

    def stop(self) -> None:
        self._live.stop()


class _EscapeWatcher:
    """Best effort: a thread that sets ``cancel`` when Esc is pressed on a TTY."""

    def __init__(self, cancel: threading.Event) -> None:
        self._cancel = cancel
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="aisquare-esc", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        # Wait for the thread: its ``finally`` puts the terminal back into
        # line mode, and a daemon thread killed at interpreter exit never
        # runs it. The join is bounded by the watcher's own select() timeout.
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=0.5)

    def _run(self) -> None:
        try:
            if sys.platform == "win32":
                self._run_windows()
            else:
                self._run_posix()
        except Exception:  # a broken terminal must never break the sign-in; Ctrl-C still works
            return

    def _run_windows(self) -> None:
        if sys.platform == "win32":
            import msvcrt

            while not self._stop.is_set():
                if msvcrt.kbhit() and msvcrt.getwch() == "\x1b":
                    self._cancel.set()
                    return
                time.sleep(0.05)

    def _run_posix(self) -> None:
        import select
        import termios
        import tty

        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while not self._stop.is_set():
                ready, _, _ = select.select([fd], [], [], 0.1)
                if ready and os.read(fd, 1) == b"\x1b":
                    self._cancel.set()
                    return
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def _emit_signed_in(session: iam.Session) -> None:
    if get_state().json_output:
        typer.echo(json.dumps(session.as_json()))
        return
    who = session.email or session.sub or "you"
    stdout_console().print(f"✓ Signed in as {who} ({iam.device_name()})")
    if session.expires_at is not None:
        when = session.expires_at.astimezone().strftime("%b %d, %Y")
        stdout_console().print(
            f"  This session expires {when}. Sign out any time with aisquare logout."
        )


# --------------------------------------------------------------------------- logout / whoami


def logout() -> None:
    """Sign out: revoke the session on the server and forget it here."""
    env_set = bool(os.environ.get(iam.TOKEN_ENV_VAR, "").strip())
    session = iam.stored_session()
    if session is None:
        if get_state().json_output:
            typer.echo(
                json.dumps(
                    {"signed_out": False, "server_revoked": False, "env_token_still_set": env_set}
                )
            )
        else:
            _say("Not signed in.")
            if env_set:
                _say(f"⚠ {iam.TOKEN_ENV_VAR} is still set in this shell.")
        return
    revoked = auth_service.sign_out(session)
    if get_state().json_output:
        typer.echo(
            json.dumps(
                {"signed_out": True, "server_revoked": revoked, "env_token_still_set": env_set}
            )
        )
        return
    if revoked:
        _say("✓ Signed out. The session was revoked on the server.")
    else:
        _say(
            "✓ Signed out on this machine. Couldn't reach the server. "
            "Revoke this session from Settings > Security."
        )
    if env_set:
        _say(f"⚠ {iam.TOKEN_ENV_VAR} is still set in this shell.")


def whoami() -> None:
    """Show which account this machine is signed in as (offline)."""
    try:
        session = iam.current_session()
    except iam.IamError as exc:
        _fail(exc)
    if session is None:
        fail("Not signed in. Run aisquare login.", error="not_authenticated")
    if get_state().json_output:
        typer.echo(json.dumps(session.as_json()))
        return
    if session.source == "env":
        stdout_console().print(f"token from {iam.TOKEN_ENV_VAR} · {session.api_url}")
        return
    days = session.expires_in_days()
    expiry = f"expires in {days} days" if days is not None else "no recorded expiry"
    stdout_console().print(f"{session.email or session.sub} · {session.api_url} · {expiry}")


# --------------------------------------------------------------------------- aisquare auth


@app.command("status")
def status(live: LiveFlag = False) -> None:
    """Show whether this machine is signed in, and as whom."""
    try:
        session = iam.current_session()
    except iam.IamError as exc:
        _fail(exc)
    report: dict[str, object] = {
        "signed_in": session is not None,
        "source": session.source if session else None,
        "api_url": session.api_url if session else None,
        "user": session.as_json()["user"] if session else None,
        "expires_at": session.expires_at.isoformat() if session and session.expires_at else None,
        "live": None,
    }
    if session is not None and live:
        report["live"] = auth_service.live_check(session)
    if get_state().json_output:
        typer.echo(json.dumps(report))
    elif session is None:
        _say("Not signed in. Run aisquare login.")
    else:
        who = session.email or session.sub or f"token from {iam.TOKEN_ENV_VAR}"
        _say(f"✓ Signed in as {who} ({session.source}) · {session.api_url}")
        if session.expires_at is not None:
            _say(f"  Expires {session.expires_at.astimezone().strftime('%b %d, %Y')}")
        live_report = report["live"]
        if isinstance(live_report, dict):
            if live_report.get("ok"):
                _say("  Server check: the session works.")
            else:
                _say(f"  Server check: {live_report.get('message') or live_report.get('error')}")
    if session is None:
        raise typer.Exit(1)


@app.command("token")
def token() -> None:
    """Print the access token, for scripting. Treat it like a password."""
    try:
        session = iam.current_session()
    except iam.IamError as exc:
        _fail(exc)
    if session is None:
        fail("Not signed in. Run aisquare login.", error="not_authenticated")
    if get_state().json_output:
        typer.echo(json.dumps({"token": session.token}))
        return
    if sys.stdout.isatty():
        stderr_console().print(
            "⚠ This token grants full access to your account "
            "and is now in your terminal scrollback."
        )
    typer.echo(session.token)
