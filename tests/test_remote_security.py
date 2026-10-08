"""The nine security findings of #243, closed (SPEC §2): one test or more per behaviour.

Keys reach tmux only from an allowlist; the unlock limiter keys on uvicorn's peer and a
global budget bounds guesses without evaluating them; devices have ids that are not
cookies; idle signs a device out and age removes it; auto-off is the server's to keep;
the audit trail cannot be forged; ``project/add`` stays inside the home directory; a
write or a socket must come from the page's own origin. Everything runs on fakes: no
network, no real tmux, a wall clock moved by hand where time matters.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import re
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from starlette.testclient import TestClient, WebSocketDenialResponse
from typer.testing import CliRunner

from aisquare.cli.app import app as cli
from aisquare.core.paths import remote_audit_path, remote_state_path
from aisquare.core.store import store_session
from aisquare.core.tmux import TmuxError
from aisquare.core.workspace import project_id_for
from aisquare.models import ProjectInfo
from aisquare.services import fleet as fleet_service
from aisquare.services import project as project_service
from aisquare.services import remote_push, remote_server
from aisquare.services.remote_server import (
    AUTO_OFF_CHECK_SECONDS,
    COOKIE,
    DEVICE_ID,
    EXIT_KEY_REPEAT_SECONDS,
    KNOWN_DEVICE_FAILURES_MAX,
    LINK_GONE,
    NOTE_TEXT_MAX,
    REMOTE_KEY_NAME,
    SEND_KEYS_KEYS_MAX,
    SEND_KEYS_TEXT_MAX,
    UNLOCK_GLOBAL_FAILURES,
    WS_CLOSE_REMOTE_OFF,
    WS_CLOSE_UNAUTHORIZED,
    RequestError,
    Runtime,
    Sources,
    UnlockBudget,
    Writes,
    _audit_clean,
    _AutoOffTimer,
    _RateLimiter,
    allowed_origin,
    build_app,
    check_project_add_root,
    check_remote_key_names,
    is_direct_loopback,
    live_writes,
    normalize_passphrase,
)
from aisquare.services.remote_words import REMOTE_PASSPHRASE_WORDS
from tests.remote_kit_helpers import (
    PASSWORD,
    base,
    make_client,
    make_runtime,
    receive_within,
    unlock,
)

PAD_KEYS = [
    *("Enter", "Escape", "Tab", "BTab", "BSpace", "Space", "Up", "Down", "Left", "Right"),
    *("Home", "End", "PageUp", "PageDown", "Delete", *(f"F{n}" for n in range(1, 13))),
    *("C-c", "C-d", "C-l", "C-o", "C-r", "C-u", *(str(n) for n in range(10)), "y", "n"),
]
"""Every key the page's pad sends (SPEC §6.3), which the allowlist must take."""


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
    """The built app over the fixture's runtime, every write a no-op that goes through."""

    def handler(body: dict[str, Any]) -> tuple[dict[str, object], str]:
        return {"ok": True}, "ran"

    writes = Writes({name: handler for name in remote_server.write_endpoint_names()})
    return build_app(runtime, sources=_sources(), writes=writes, dist_dir=tmp_path, tick=0.02)


class Clock:
    """The wall clock every expiry and budget reads (``remote_server._remote_now``)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.now = datetime(2026, 10, 7, 9, 0, tzinfo=UTC)
        monkeypatch.setattr(remote_server, "_remote_now", lambda: self.now)

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    return Clock(monkeypatch)


def _from(app: Any, host: str, **kw: Any) -> TestClient:
    """A client whose peer, as uvicorn resolved it, is ``host`` (SPEC §0.3)."""
    return make_client(app, client=(host, 4000), **kw)


def _audit_lines() -> list[list[str]]:
    path = remote_audit_path()
    if not path.exists():
        return []
    return [line.split(" ", 3) for line in path.read_text(encoding="utf-8").splitlines()]


# --- (1) send-keys: the allowlist, the double press, one input per body -----------------


@pytest.mark.parametrize("key", PAD_KEYS)
def test_every_pad_key_is_accepted(key: str) -> None:
    assert check_remote_key_names([key]) == [key]


@pytest.mark.parametrize(
    "key",
    [";", "-l", "kill-server", "C-c;", "Enter\n", "C-z", "C-a", "M-x", "F13", "F0", "", "enter"],
)
def test_anything_else_is_refused_by_name_with_the_vocabulary(key: str) -> None:
    """Measured on tmux 3.7c before this: ``[";", "run-shell", …]`` ran a shell command
    and ``["Enter;", "kill-server"]`` killed every agent on the server (review of #243)."""
    with pytest.raises(RequestError) as refused:
        check_remote_key_names(["Enter", key])
    assert (refused.value.status, refused.value.error) == (400, "invalid_key")
    assert "\n" not in refused.value.message
    assert "C-c, C-d, C-l, C-o, C-r, C-u" in refused.value.message


def test_the_allowlist_is_used_whole_never_as_a_prefix() -> None:
    """``fullmatch`` with ``\\Z``: a key with anything after a pad key is not that key."""
    assert REMOTE_KEY_NAME.fullmatch("Enter")
    assert not REMOTE_KEY_NAME.fullmatch("Enter;")
    assert not REMOTE_KEY_NAME.fullmatch("Enter\n")
    assert REMOTE_KEY_NAME.match("Enter\n") is None, "\\Z, not $: no newline sneaks past"


def test_a_refused_key_is_named_scrubbed_and_cut_to_32_characters() -> None:
    with pytest.raises(RequestError) as refused:
        check_remote_key_names(["x\x1b[2J" + "y" * 60])
    named = refused.value.message.split("'")[1]
    assert len(named) == 32 and named.startswith("x?[2J") and named.endswith("…")


def test_more_keys_than_the_cap_is_413_and_not_a_list_is_400() -> None:
    assert check_remote_key_names(["Up"] * SEND_KEYS_KEYS_MAX) == ["Up"] * SEND_KEYS_KEYS_MAX
    with pytest.raises(RequestError) as too_many:
        check_remote_key_names(["Up"] * (SEND_KEYS_KEYS_MAX + 1))
    assert (too_many.value.status, too_many.value.error) == (413, "too_large")
    for keys in ("Enter", [1], None, {"Enter": 1}):
        with pytest.raises(RequestError) as refused:
            check_remote_key_names(keys)
        assert refused.value.error == "invalid_key"


class FakePane:
    """The tmux server ``send-keys`` reaches: records what arrived, fails when told to.

    Its pane runs the agent, on the server the row was recorded on: it started
    before the row was written."""

    STARTED = datetime(2026, 10, 7, 8, 0, tzinfo=UTC)

    def __init__(self) -> None:
        self.sent: list[tuple[str, ...]] = []
        self.fail_keys = False

    def pane_facts(self, pane_id: str) -> SimpleNamespace:
        return SimpleNamespace(dead=False, current_command="claude")

    def started_at(self) -> datetime:
        return self.STARTED

    def send_literal(self, pane_id: str, text: str) -> None:
        self.sent.append(("literal", text))

    def send_keys(self, pane_id: str, *keys: str) -> None:
        if self.fail_keys:
            raise TmuxError("can't find pane: %1")
        self.sent.append(("keys", *keys))


@pytest.fixture
def pane(monkeypatch: pytest.MonkeyPatch) -> FakePane:
    """One live agent ``coder-1`` in project ``prj_p``, whose pane is a :class:`FakePane`."""
    import aisquare.core.store as store_module

    fake = FakePane()

    class Agent:
        pane_id = "%1"
        tmux_socket = "asq"
        created_at = FakePane.STARTED + timedelta(hours=1)

    class Store:
        def fleet_agent_by_label(self, *args: object, **kwargs: object) -> Agent:
            return Agent()

        def __enter__(self) -> Store:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    class Project:
        id = "prj_p"
        root = Path("/tmp/p")

    monkeypatch.setattr(remote_server, "_resolve_project", lambda ref: Project())
    monkeypatch.setattr(store_module, "store_session", lambda: Store())
    monkeypatch.setattr(fleet_service, "server_for", lambda socket, config=None: fake)
    return fake


def test_a_second_ctrl_c_to_one_agent_within_3_s_is_a_double_press(
    pane: FakePane, clock: Clock
) -> None:
    """Claude Code exits on a second Ctrl-C ("Press Ctrl-C again to exit"), and an exit ends
    the row, releases its claims and wakes the manager."""
    send = live_writes().handlers["send-keys"]
    send({"agent": "coder-1", "keys": ["C-c"]})
    clock.advance(seconds=EXIT_KEY_REPEAT_SECONDS - 0.5)
    for key in ("C-c", "C-d"):
        with pytest.raises(RequestError) as refused:
            send({"agent": "coder-1", "keys": [key]})
        assert (refused.value.status, refused.value.error) == (409, "double_press")
        assert "confirm_exit" in refused.value.message
    assert pane.sent == [("keys", "C-c")], "the refused presses sent nothing"
    send({"agent": "coder-1", "keys": ["C-c"], "confirm_exit": True})
    clock.advance(seconds=EXIT_KEY_REPEAT_SECONDS)
    send({"agent": "coder-1", "keys": ["C-d"]})
    assert pane.sent == [("keys", "C-c"), ("keys", "C-c"), ("keys", "C-d")]


def test_two_exit_keys_in_one_body_are_a_double_press_unless_confirmed(pane: FakePane) -> None:
    send = live_writes().handlers["send-keys"]
    with pytest.raises(RequestError) as refused:
        send({"agent": "coder-1", "keys": ["C-c", "C-c"]})
    assert refused.value.error == "double_press" and pane.sent == []
    send({"agent": "coder-1", "keys": ["C-c", "C-d"], "confirm_exit": True})
    assert pane.sent == [("keys", "C-c", "C-d")]


def test_text_and_keys_in_one_body_are_refused_and_text_is_capped(pane: FakePane) -> None:
    """The handler sends text first, so "Esc, then type" arrived as "type, then Esc"."""
    send = live_writes().handlers["send-keys"]
    with pytest.raises(RequestError) as both:
        send({"agent": "coder-1", "text": "hi", "keys": ["Escape"]})
    assert (both.value.status, both.value.error) == (400, "text_and_keys")
    with pytest.raises(RequestError) as long:
        send({"agent": "coder-1", "text": "x" * (SEND_KEYS_TEXT_MAX + 1)})
    assert (long.value.status, long.value.error) == (413, "too_large")
    assert pane.sent == []
    send({"agent": "coder-1", "text": "x" * SEND_KEYS_TEXT_MAX, "enter": True})
    assert pane.sent == [("literal", "x" * SEND_KEYS_TEXT_MAX), ("keys", "Enter")]


@pytest.mark.parametrize(
    ("char", "said"),
    [
        ("\x03", "the pad's C-c key"),
        ("\x04", "the pad's C-d key"),
        ("\x1b", "the pad's Escape key"),
        ("\x7f", "the pad's BSpace key"),
        ("\x1a", "no key of the pad sends it"),
        ("\x00", "no key of the pad sends it"),
    ],
    ids=repr,
)
def test_text_holding_a_control_character_is_refused_and_names_the_key(
    pane: FakePane, char: str, said: str
) -> None:
    """Text reaches the pane byte for byte: ``"\\x03"`` twice was two Ctrl-Cs past the
    double-press guard, and ``"\\x1a"`` the Ctrl-Z no key may send, each audited as
    ``text=1ch`` (review of #243, round 2)."""
    send = live_writes().handlers["send-keys"]
    with pytest.raises(RequestError) as refused:
        send({"agent": "coder-1", "text": f"yes{char}"})
    assert (refused.value.status, refused.value.error) == (400, "invalid")
    assert f"U+{ord(char):04X}" in refused.value.message and said in refused.value.message
    assert pane.sent == []


def test_tab_newline_and_carriage_return_are_still_text(pane: FakePane) -> None:
    send = live_writes().handlers["send-keys"]
    send({"agent": "coder-1", "text": "a\tb\nc\r"})
    assert pane.sent == [("literal", "a\tb\nc\r")]


def test_a_send_that_fails_after_typing_is_still_on_the_audit_trail(
    runtime: Runtime, pane: FakePane, tmp_path: Path
) -> None:
    """The text reached the pane, then Enter failed: the write answered 400 and used to
    leave no audit line, though a live agent had been typed into (review of #243)."""
    client = make_client(build_app(runtime, sources=_sources(), dist_dir=tmp_path))
    device_id = unlock(client, runtime).json()["device"]["id"]
    runtime.set_allow_write(True)
    pane.fail_keys = True
    response = client.post(
        f"{base(runtime)}/api/send-keys", json={"agent": "coder-1", "text": "rm -rf", "enter": True}
    )
    assert response.status_code == 400 and response.json()["error"] == "write_failed"
    assert pane.sent == [("literal", "rm -rf")]
    _ts, who, endpoint, summary = _audit_lines()[-1]
    assert (who, endpoint) == (device_id, "send-keys")
    assert summary == "coder-1@prj_p text=6ch keys=0 enter=True failed"


def test_a_key_outside_the_allowlist_sends_nothing_at_all(pane: FakePane) -> None:
    send = live_writes().handlers["send-keys"]
    with pytest.raises(RequestError):
        send({"agent": "coder-1", "keys": ["Enter;", "kill-server"], "enter": True})
    assert pane.sent == []


# --- (2) unlock: the peer, the global budget, the known device, the machine ------------


def test_the_limiter_keys_on_the_resolved_peer_not_a_header(app: Any, runtime: Runtime) -> None:
    """A fresh ``X-Forwarded-For`` per request got 20 of 20 guesses through (review of #243)."""
    client = _from(app, "203.0.113.5")
    for n in range(5):
        forged = {"X-Forwarded-For": f"198.51.100.{n}"}
        response = client.post(
            f"{base(runtime)}/api/unlock", json={"password": "wrong"}, headers=forged
        )
        assert response.status_code == 401
    refused = client.post(
        f"{base(runtime)}/api/unlock",
        json={"password": PASSWORD},
        headers={"X-Forwarded-For": "198.51.100.99"},
    )
    assert refused.status_code == 429 and refused.json()["error"] == "too_many_attempts"
    assert 1 <= int(refused.headers["retry-after"]) <= 60


def test_the_limiter_forgets_a_client_whose_minute_is_over() -> None:
    """Keyed on invented addresses, the table grew by one entry per request, forever."""
    now = [0.0]
    limiter = _RateLimiter(lambda: now[0])
    for n in range(500):
        assert limiter.limiter_retry_after(f"198.51.{n // 250}.{n % 250}") is None
    now[0] += 61
    assert limiter.limiter_retry_after("203.0.113.5") is None
    assert list(limiter._attempts) == ["203.0.113.5"]


def test_the_client_is_the_scope_peer_and_only_that() -> None:
    scope = {"client": ("203.0.113.5", 1), "headers": [(b"x-forwarded-for", b"10.0.0.1")]}
    assert remote_server._client_of(scope) == "203.0.113.5"
    assert remote_server._client_of({"headers": []}) == "unknown"


def _spend_the_budget(app: Any, runtime: Runtime) -> None:
    """``UNLOCK_GLOBAL_FAILURES`` wrong guesses from as many addresses, five a minute each."""
    for n in range(UNLOCK_GLOBAL_FAILURES):
        assert unlock(_from(app, f"203.0.113.{n}"), runtime, "wrong").status_code == 401


def test_a_spent_budget_refuses_a_stranger_without_evaluating_the_guess(
    app: Any, runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even the right passphrase: if it got through, a wrong one would still say "wrong",
    and the budget would bound nothing (SPEC §9.1)."""
    _spend_the_budget(app, runtime)
    evaluated: list[str] = []
    real = runtime.password_matches

    def counted(supplied: str) -> bool:
        evaluated.append(supplied)
        return real(supplied)

    monkeypatch.setattr(runtime, "password_matches", counted)
    refused = unlock(_from(app, "198.51.100.7"), runtime, PASSWORD)
    assert refused.status_code == 429 and refused.json()["error"] == "locked_out"
    assert "regenerate-password --new-link" in refused.json()["message"]
    assert 1700 < int(refused.headers["retry-after"]) <= 1800
    assert evaluated == [], "the passphrase was never compared"
    assert runtime.device_rows() == []


def test_the_budget_holds_across_processes_and_restarts(app: Any, runtime: Runtime) -> None:
    """Kept in remote.json: ``asq remote status`` in another shell sees what the server
    enforces, and a restart does not hand out twenty more guesses."""
    _spend_the_budget(app, runtime)
    shell = Runtime(remote_state_path(), remote_audit_path())
    assert UnlockBudget(shell).budget_failures() == UNLOCK_GLOBAL_FAILURES
    assert UnlockBudget(shell).budget_exhausted_until() is not None
    restarted = build_app(shell, sources=_sources(), dist_dir=Path())
    assert unlock(_from(restarted, "198.51.100.8"), shell).status_code == 429


def test_the_budget_ages_out_and_only_a_password_change_resets_it(
    app: Any, runtime: Runtime, clock: Clock
) -> None:
    _spend_the_budget(app, runtime)
    assert unlock(_from(app, "198.51.100.1"), runtime).status_code == 429
    direct = _from(app, "127.0.0.1", base_url="http://127.0.0.1:8750")
    assert unlock(direct, runtime).status_code == 200, "a success resets nothing…"
    assert unlock(_from(app, "198.51.100.2"), runtime).status_code == 429, "…as here"
    clock.advance(minutes=30, seconds=1)
    assert unlock(_from(app, "198.51.100.3"), runtime).status_code == 200, "aged out"
    _spend_the_budget(app, runtime)
    runtime.regenerate_password()
    assert UnlockBudget(runtime).budget_failures() == 0
    assert unlock(_from(app, "198.51.100.4"), runtime, runtime.password).status_code == 200


def test_the_machine_itself_always_unlocks_and_its_guesses_do_not_count(
    app: Any, runtime: Runtime
) -> None:
    _spend_the_budget(app, runtime)
    direct = _from(app, "127.0.0.1", base_url="http://127.0.0.1:8750")
    assert unlock(direct, runtime, "wrong").status_code == 401
    assert UnlockBudget(runtime).budget_failures() == UNLOCK_GLOBAL_FAILURES
    assert unlock(direct, runtime).status_code == 200


@pytest.mark.parametrize(
    ("peer", "host", "headers", "direct"),
    [
        ("127.0.0.1", "127.0.0.1:8750", [], True),
        ("::1", "[::1]:8750", [], True),
        ("127.0.0.1", "localhost", [], True),
        ("127.0.0.1", "abcd-12.ngrok-free.app", [], False),
        ("127.0.0.1", "127.0.0.1:8750", [(b"x-forwarded-for", b"203.0.113.5")], False),
        ("127.0.0.1", "127.0.0.1:8750", [(b"x-forwarded-proto", b"https")], False),
        ("127.0.0.1", "127.0.0.1:8750", [(b"x-forwarded-host", b"x")], False),
        ("127.0.0.1", "127.0.0.1:8750", [(b"forwarded", b"for=1.2.3.4")], False),
        ("203.0.113.5", "127.0.0.1:8750", [], False),
    ],
)
def test_direct_means_the_loopback_with_no_hop_in_between(
    peer: str, host: str, headers: list[tuple[bytes, bytes]], direct: bool
) -> None:
    scope = {"client": (peer, 1), "headers": [(b"host", host.encode()), *headers]}
    assert is_direct_loopback(scope) is direct


def test_a_known_phone_unlocks_through_a_spent_budget_into_its_own_device(
    app: Any, runtime: Runtime, clock: Clock
) -> None:
    """The DoS relief (SPEC §2.2 item 4): the human's own phone, signed out by a quiet day,
    re-unlocks into the same id while strangers are paused."""
    phone = _from(app, "198.51.100.50")
    first = unlock(phone, runtime)
    device_id, old_secret = first.json()["device"]["id"], phone.cookies[COOKIE]
    clock.advance(hours=25)  # idle: signed out
    assert phone.get(f"{base(runtime)}/api/board").status_code == 401
    _spend_the_budget(app, runtime)
    again = unlock(phone, runtime)
    assert again.status_code == 200
    assert again.json()["device"] == first.json()["device"], "same id, same expiry"
    assert phone.cookies[COOKIE] != old_secret, "a new secret"
    assert "Max-Age=514800" in again.headers["set-cookie"], "what is left of its 7 days"
    assert phone.get(f"{base(runtime)}/api/board").status_code == 200
    stale = make_client(app)
    stale.cookies.set(COOKIE, old_secret)
    assert stale.get(f"{base(runtime)}/api/board").status_code == 401
    assert _audit_lines()[-1][2:] == ["unlock", f"device {device_id} reactivated"]


def test_a_known_cookie_buys_ten_guesses_then_its_device_is_revoked(
    app: Any, runtime: Runtime, caplog: pytest.LogCaptureFixture
) -> None:
    """The revoke is said in the log and on the audit trail: it is most likely a stolen
    cookie, and its owner found only a device gone (review of #243, round 2)."""
    phone = _from(app, "198.51.100.51")
    device_id = unlock(phone, runtime).json()["device"]["id"]
    _spend_the_budget(app, runtime)
    with caplog.at_level("WARNING", logger=remote_server.__name__):
        for n in range(KNOWN_DEVICE_FAILURES_MAX):
            guesser = _from(app, f"198.51.100.{60 + n}")  # its own limiter row each time
            guesser.cookies.set(COOKIE, phone.cookies[COOKIE])
            assert unlock(guesser, runtime, "wrong").status_code == 401
    assert runtime.device_rows() == [], "the tenth wrong guess revoked it"
    assert UnlockBudget(runtime).budget_failures() == UNLOCK_GLOBAL_FAILURES, "none counted"
    assert unlock(phone, runtime).status_code == 429, "a stranger again"
    said = f"device {device_id} revoked after {KNOWN_DEVICE_FAILURES_MAX} wrong passwords"
    assert [r for r in caplog.records if said in r.getMessage()], "logged"
    _ts, who, endpoint, summary = _audit_lines()[-1]
    assert (who, endpoint) == (device_id, "unlock") and summary.startswith(said)


def test_a_right_guess_resets_the_known_devices_count(app: Any, runtime: Runtime) -> None:
    phone = _from(app, "198.51.100.52")
    unlock(phone, runtime)
    for n in range(KNOWN_DEVICE_FAILURES_MAX - 1):
        guesser = _from(app, f"198.51.100.{80 + n}")
        guesser.cookies.set(COOKIE, phone.cookies[COOKIE])
        unlock(guesser, runtime, "wrong")
    assert unlock(phone, runtime).status_code == 200
    assert runtime._state.devices[0].failed_unlocks == 0


def test_a_wrong_guess_takes_no_lock_on_remote_json(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``unlock_device`` took the file lock merely to compare the passphrase, so a wrong guess
    waited on another process holding it, and then waited again to be counted."""
    taken: list[Path] = []

    def lock_and_record(path: Path) -> None:
        taken.append(path)  # and goes on without the lock, as a stalled holder would let it

    monkeypatch.setattr(remote_server, "_lock_state_file", lock_and_record)
    assert runtime.unlock_device("wrong", "Pixel") is None and taken == []
    assert runtime.unlock_device(PASSWORD, "Pixel") is not None
    assert taken == [remote_state_path()]


def test_a_guess_waiting_on_another_processs_lock_holds_up_no_other_request(
    app: Any, runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unlock's writes ran on the event loop: one wrong guess while a CLI command held
    ``remote.json.lock`` stalled every socket and every read for seconds."""
    from aisquare.core.locking import lock_exclusive
    from aisquare.core.locking import unlock as release

    monkeypatch.setattr(remote_server, "STATE_LOCK_WAIT_SECONDS", 3.0)
    waiting = threading.Event()
    lock_state_file = remote_server._lock_state_file

    def lock_and_say(path: Path) -> int | None:
        waiting.set()
        return lock_state_file(path)

    monkeypatch.setattr(remote_server, "_lock_state_file", lock_and_say)
    fd = os.open(remote_state_path().with_name("remote.json.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    lock_exclusive(fd)  # what the CLI command holds
    answers: list[int] = []
    with _from(app, "203.0.113.70") as stranger:  # one event loop serves both requests
        guess = threading.Thread(
            target=lambda: answers.append(unlock(stranger, runtime, "wrong").status_code)
        )
        guess.start()
        try:
            assert waiting.wait(5), "the wrong guess is counted under the lock"
            started = time.monotonic()
            read = stranger.get(f"{base(runtime)}/api/remote")
            took = time.monotonic() - started
            assert read.status_code == 401 and took < 1.5, f"the read waited {took:.1f} s"
            assert answers == [], "answered while the guess still waited"
        finally:
            release(fd)
            os.close(fd)
        guess.join(10)
    assert answers == [401]


def test_guesses_decided_at_once_get_no_further_past_the_budget(app: Any, runtime: Runtime) -> None:
    """Off the event loop, unlocks are still decided one at a time: with one guess left in
    the budget, five guesses together get one evaluation and four ``locked_out``."""
    for n in range(UNLOCK_GLOBAL_FAILURES - 1):
        unlock(_from(app, f"203.0.113.{n}"), runtime, "wrong")
    together = threading.Barrier(5, timeout=10)  # a guess that fails first breaks it, not hangs
    answers: list[int] = []

    def guess(n: int) -> None:
        client = _from(app, f"198.51.100.{140 + n}")
        together.wait()
        answers.append(unlock(client, runtime, "wrong").status_code)

    guesses = [threading.Thread(target=guess, args=(n,)) for n in range(5)]
    for thread in guesses:
        thread.start()
    for thread in guesses:
        thread.join(30)
    assert sorted(answers) == [401, 429, 429, 429, 429]
    assert UnlockBudget(runtime).budget_failures() == UNLOCK_GLOBAL_FAILURES


def test_the_trip_is_said_once_logged_and_pushed(
    app: Any, runtime: Runtime, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    alerts: list[tuple[list[str], str]] = []
    monkeypatch.setattr(
        remote_push, "push_security_alert", lambda ids, text: alerts.append((list(ids), text))
    )
    phone = _from(app, "198.51.100.53")
    device_id = unlock(phone, runtime).json()["device"]["id"]
    with caplog.at_level("WARNING", logger=remote_server.__name__):
        _spend_the_budget(app, runtime)
        for n in range(3):  # refused, not counted: no second trip
            unlock(_from(app, f"198.51.100.{120 + n}"), runtime, "wrong")
    assert alerts == [([device_id], remote_server.LOCKOUT_ALERT)]
    said = [r.getMessage() for r in caplog.records if "new unlocks are paused" in r.getMessage()]
    assert len(said) == 1 and "regenerate-password --new-link" in said[0]


def test_status_reports_the_failed_unlocks_and_the_lockout(
    app: Any, runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(remote_server, "_runtime", None)
    for n in range(3):
        unlock(_from(app, f"203.0.113.{n}"), runtime, "wrong")
    status = remote_server.remote_server_status()
    assert (status["failed_unlocks"], status["locked_out_until"]) == (3, None)
    for n in range(3, UNLOCK_GLOBAL_FAILURES):
        unlock(_from(app, f"203.0.113.{n}"), runtime, "wrong")
    shown = json.loads(CliRunner().invoke(cli, ["--json", "remote", "status"]).stdout)
    assert shown["failed_unlocks"] == UNLOCK_GLOBAL_FAILURES
    assert shown["locked_out_until"] == remote_server.remote_server_status()["locked_out_until"]
    human = CliRunner().invoke(cli, ["remote", "status"]).stdout
    assert "new unlocks paused until" in human and "--new-link" in human


# --- the passphrase -----------------------------------------------------------------


def test_the_word_list_is_512_distinct_common_words_of_3_to_7_letters() -> None:
    assert len(REMOTE_PASSPHRASE_WORDS) == 512
    assert len(set(REMOTE_PASSPHRASE_WORDS)) == 512
    assert list(REMOTE_PASSPHRASE_WORDS) == sorted(REMOTE_PASSPHRASE_WORDS)
    assert all(re.fullmatch(r"[a-z]{3,7}", word) for word in REMOTE_PASSPHRASE_WORDS)


def test_a_passphrase_is_four_distinct_words_of_the_list() -> None:
    for _ in range(50):
        words = remote_server.new_password().split("-")
        assert len(words) == 4 == len(set(words))
        assert set(words) <= set(REMOTE_PASSPHRASE_WORDS)


@pytest.mark.parametrize(
    ("typed", "normalized"),
    [
        ("Amber River, cedar  DELTA", "amber-river-cedar-delta"),
        ("amber-river-cedar-delta", "amber-river-cedar-delta"),
        (" amber river\tcedar delta\n", "amber-river-cedar-delta"),
        ("Amber.River.Cedar.Delta", "amber-river-cedar-delta"),
    ],
)
def test_a_phone_typed_passphrase_is_normalized(typed: str, normalized: str) -> None:
    assert normalize_passphrase(typed) == normalized


def test_password_matches_the_phone_typed_form_but_never_widens_a_non_phrase(
    runtime: Runtime,
) -> None:
    runtime._state.password = "amber-river-cedar-delta"
    runtime._save_state()
    assert runtime.password_matches("Amber River, cedar  DELTA")
    assert runtime.password_matches("amber-river-cedar-delta")
    assert not runtime.password_matches("amber river cedar")
    runtime._state.password = "Test1234"
    runtime._save_state()
    assert runtime.password_matches("Test1234")
    assert not runtime.password_matches("Test1235"), "normalizing both sides would match"
    assert not runtime.password_matches("test1234")


def test_a_version_1_file_is_migrated_once(isolated_home: Path) -> None:
    """Its sessions were stored as raw cookies and its password came from 32 words: both
    go. The link and the switches stay (SPEC §2.2 item 10)."""
    remote_state_path().parent.mkdir(parents=True, exist_ok=True)
    old = {
        "token": "T" * 32,
        "password": "amber-birch-cedar-delta",
        "allow_write": True,
        "auto_off_at": "2026-10-07T18:00:00+00:00",
        "sessions": [{"sid": "raw-cookie-value", "ua": "iPhone", "first_seen": "x"}],
    }
    remote_state_path().write_text(json.dumps(old), encoding="utf-8")
    migrated = Runtime(remote_state_path(), remote_audit_path())
    raw = json.loads(remote_state_path().read_text(encoding="utf-8"))
    assert raw["version"] == 2 and raw["devices"] == []
    assert (raw["token"], raw["allow_write"], raw["auto_off_at"]) == (
        old["token"],
        True,
        old["auto_off_at"],
    )
    assert raw["password"] != old["password"]
    assert set(raw["password"].split("-")) <= set(REMOTE_PASSPHRASE_WORDS)
    assert "raw-cookie-value" not in remote_state_path().read_text(encoding="utf-8")
    assert migrated.password == raw["password"]
    again = Runtime(remote_state_path(), remote_audit_path())
    assert again.password == raw["password"], "once"


@pytest.mark.parametrize("value", ["false", "off", "no", "true", 1, ["on"]], ids=repr)
def test_writes_are_on_only_for_a_json_true(isolated_home: Path, value: object) -> None:
    """``bool()`` read a hand edit's ``"false"`` as true: every write opened, ``api/remote``
    told the page so, and the load wrote ``"allow_write": true`` back (review of #243,
    round 2)."""
    server = Runtime(remote_state_path(), remote_audit_path())
    raw = json.loads(remote_state_path().read_bytes())
    raw["allow_write"] = value
    remote_state_path().write_text(json.dumps(raw, indent=2), encoding="utf-8")
    assert server.allow_write is False, "the running server adopts the edit as off"
    assert Runtime(remote_state_path(), remote_audit_path()).allow_write is False
    assert json.loads(remote_state_path().read_bytes())["allow_write"] is False


def test_a_version_1_file_keeps_writes_on_only_for_a_json_true(isolated_home: Path) -> None:
    remote_state_path().parent.mkdir(parents=True, exist_ok=True)
    old = {"token": "T" * 32, "password": "x", "allow_write": "false", "sessions": []}
    remote_state_path().write_text(json.dumps(old), encoding="utf-8")
    assert Runtime(remote_state_path(), remote_audit_path()).allow_write is False
    assert json.loads(remote_state_path().read_bytes())["allow_write"] is False


# --- (3) device ids that are not cookies ----------------------------------------------


def test_a_device_is_named_by_an_id_and_only_its_digest_is_stored(
    app: Any, runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``GET api/devices`` listed every device's sid, which WAS its cookie: any unlocked
    phone, read-only included, could keep using another's after it was revoked."""
    monkeypatch.setattr(remote_server, "_runtime", runtime)
    mine, theirs = make_client(app), make_client(app)
    unlock(mine, runtime)
    unlock(theirs, runtime)
    secrets_ = [mine.cookies[COOKIE], theirs.cookies[COOKIE]]
    digests = [hashlib.sha256(secret.encode()).hexdigest() for secret in secrets_]
    stored = remote_state_path().read_text(encoding="utf-8")
    assert all(secret not in stored for secret in secrets_)
    assert [d["secret_sha256"] for d in json.loads(stored)["devices"]] == digests
    listed = mine.get(f"{base(runtime)}/api/devices").text
    rows = json.loads(listed)
    assert all(DEVICE_ID.fullmatch(row["id"]) for row in rows)
    for leak in (*secrets_, *digests):
        assert leak not in listed
        assert leak not in remote_audit_path().read_text(encoding="utf-8")
        assert leak not in CliRunner().invoke(cli, ["--json", "remote", "status"]).stdout
        assert leak not in CliRunner().invoke(cli, ["remote", "status"]).stdout


def test_signing_out_is_always_allowed_and_clears_the_cookie(app: Any, runtime: Runtime) -> None:
    client = make_client(app)
    device_id = unlock(client, runtime).json()["device"]["id"]
    assert runtime.allow_write is False
    response = client.delete(f"{base(runtime)}/api/devices/{device_id}")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "id": device_id, "signed_out": True}
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert client.get(f"{base(runtime)}/api/board").status_code == 401
    assert _audit_lines()[-1][2:] == ["devices/revoke", "self"]


@pytest.mark.parametrize("device_id", ["dev_00000000", "dev_zz", "sid", "dev_0000000a0"])
def test_another_id_that_is_no_device_is_a_404(app: Any, runtime: Runtime, device_id: str) -> None:
    client = make_client(app)
    unlock(client, runtime)
    runtime.set_allow_write(True)
    response = client.delete(f"{base(runtime)}/api/devices/{device_id}")
    assert response.status_code == 404 and response.json()["error"] == "not_found"


def test_revoking_another_device_needs_writes_and_closes_its_socket(
    app: Any, runtime: Runtime
) -> None:
    mine, theirs = make_client(app), make_client(app)
    unlock(mine, runtime)
    other = unlock(theirs, runtime).json()["device"]["id"]
    with theirs.websocket_connect(f"{base(runtime)}/ws") as ws:
        receive_within(ws)
        assert mine.delete(f"{base(runtime)}/api/devices/{other}").status_code == 403
        runtime.set_allow_write(True)
        response = mine.delete(f"{base(runtime)}/api/devices/{other}")
        assert response.json() == {"ok": True, "id": other, "signed_out": False}
        assert _closed_with(ws) == WS_CLOSE_UNAUTHORIZED
    assert _audit_lines()[-1][2:] == ["devices/revoke", other]


def _closed_with(ws: Any) -> int:
    for _ in range(400):
        message = receive_within(ws)
        if message["type"] == "websocket.close":
            return int(message["code"])
    raise AssertionError("the socket never closed")


def test_the_cli_revokes_by_id_and_all_with_4401_and_no_farewell(
    app: Any, runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(remote_server, "_runtime", runtime)
    farewells: list[object] = []
    monkeypatch.setattr(remote_push, "push_farewell", lambda *a: farewells.append(a))
    phones = [make_client(app) for _ in range(3)]
    ids = [unlock(phone, runtime).json()["device"]["id"] for phone in phones]
    closed: list[int] = []
    runtime.register_socket(ids[1], closed.append)
    runtime.register_socket(ids[2], closed.append)
    one = CliRunner().invoke(cli, ["--json", "remote", "revoke", ids[0]])
    assert one.exit_code == 0 and json.loads(one.stdout) == {"revoked": ids[0]}
    every = CliRunner().invoke(cli, ["--json", "remote", "revoke", "--all"])
    assert every.exit_code == 0 and json.loads(every.stdout) == {"revoked_all": 2}
    assert closed == [WS_CLOSE_UNAUTHORIZED] * 2
    assert farewells == [], "Remote stays on: a phone unlocks again"
    assert all(p.get(f"{base(runtime)}/api/board").status_code == 401 for p in phones)
    both = CliRunner().invoke(cli, ["--json", "remote", "revoke", ids[0], "--all"])
    assert both.exit_code == 1 and json.loads(both.stdout)["error"] == "invalid_arguments"


def test_a_user_agent_that_is_rich_markup_is_printed_as_text(
    app: Any, runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``x [/b]`` raised MarkupError and took ``asq remote status`` down (review of #243)."""
    monkeypatch.setattr(remote_server, "_runtime", runtime)
    client = make_client(app, headers={"user-agent": "x [/b] [bold red]phone"})
    unlock(client, runtime)
    shown = CliRunner().invoke(cli, ["remote", "status"])
    assert shown.exit_code == 0, shown.output
    assert "x [/b] [bold red]phone" in shown.stdout


def test_a_user_agent_reaches_the_machines_terminal_without_a_control_character(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``status`` prints the user agent as text, which keeps markup out but not an escape
    sequence: ``ESC c`` resets the terminal it is printed in. A header's bytes past 0x7f
    arrive as latin-1, so ``\\x9b``, the C1 control that starts one too, gets in as well
    (the runtime is called directly: the test client re-encodes such a byte as UTF-8)."""
    monkeypatch.setattr(remote_server, "_runtime", runtime)
    unlocked = runtime.unlock_device(PASSWORD, "Phone \x1bc\x9b2J end")
    assert unlocked is not None
    _secret, device = unlocked
    assert device.ua == "Phone ?c?2J end"
    again = runtime.reactivate_device(device.id, "Tablet \x1b]0;x\x07")
    assert again is not None and again[1].ua == "Tablet ?]0;x?"
    shown = CliRunner().invoke(cli, ["remote", "status"])
    assert shown.exit_code == 0, shown.output
    assert "Tablet ?]0;x?" in shown.stdout
    assert not any(char in shown.stdout for char in "\x1b\x9b\x07")


# --- (4) idle sign-out and absolute expiry ----------------------------------------------


def test_a_day_idle_signs_a_device_out_and_keeps_its_record(
    app: Any, runtime: Runtime, clock: Clock
) -> None:
    client = make_client(app)
    device_id = unlock(client, runtime).json()["device"]["id"]
    clock.advance(hours=23, minutes=59)
    assert client.get(f"{base(runtime)}/api/board").status_code == 200, "and that was a use"
    clock.advance(hours=24)
    assert client.get(f"{base(runtime)}/api/board").status_code == 401
    with (
        pytest.raises(WebSocketDenialResponse) as denied,
        client.websocket_connect(f"{base(runtime)}/ws"),
    ):
        pass
    assert denied.value.status_code == 401
    (row,) = runtime.device_rows()
    assert (row["id"], row["signed_in"]) == (device_id, False)
    assert runtime.device_ids() == [device_id], "its push subscription keeps working"


def test_an_open_socket_closes_4401_when_its_device_goes(
    app: Any, runtime: Runtime, clock: Clock
) -> None:
    client = make_client(app)
    unlock(client, runtime)
    with client.websocket_connect(f"{base(runtime)}/ws") as ws:
        receive_within(ws)
        clock.advance(days=7)
        assert _closed_with(ws) == WS_CLOSE_UNAUTHORIZED


def test_seven_days_after_the_first_unlock_a_device_is_removed(
    app: Any, runtime: Runtime, clock: Clock
) -> None:
    client = make_client(app)
    unlock(client, runtime)
    for _day in range(7):  # used every day, so never idle long enough to be signed out
        clock.advance(hours=23)
        assert client.get(f"{base(runtime)}/api/board").status_code == 200
    clock.advance(hours=6, minutes=59)
    assert client.get(f"{base(runtime)}/api/board").status_code == 200
    clock.advance(minutes=1)  # 7 days from the first unlock, however busy
    assert client.get(f"{base(runtime)}/api/board").status_code == 401
    assert runtime.device_ids() == [] and runtime.device_rows() == []
    runtime.flush_last_seen()
    assert json.loads(remote_state_path().read_text(encoding="utf-8"))["devices"] == []


# --- (5) auto-off: the server's to keep, and the phone may extend it --------------------


def test_past_the_deadline_everything_is_a_404_like_a_wrong_token(
    app: Any, runtime: Runtime, clock: Clock
) -> None:
    client = make_client(app)
    unlock(client, runtime)
    runtime.set_auto_off(clock.now + timedelta(minutes=5))
    assert client.get(f"{base(runtime)}/api/board").status_code == 200
    clock.advance(minutes=5)
    for path in ("/", "/api/board", "/api/remote"):
        response = client.get(f"{base(runtime)}{path}")
        assert response.status_code == 404
        assert response.json() == {"error": "not_found", "message": LINK_GONE}
    assert unlock(make_client(app), runtime).status_code == 404
    with (
        pytest.raises(WebSocketDenialResponse) as denied,
        client.websocket_connect(f"{base(runtime)}/ws"),
    ):
        pass
    assert denied.value.status_code == 404


def test_an_open_socket_closes_4410_at_the_deadline(
    app: Any, runtime: Runtime, clock: Clock
) -> None:
    client = make_client(app)
    unlock(client, runtime)
    runtime.set_auto_off(clock.now + timedelta(minutes=5))
    with client.websocket_connect(f"{base(runtime)}/ws") as ws:
        receive_within(ws)
        clock.advance(minutes=5)
        assert _closed_with(ws) == WS_CLOSE_REMOTE_OFF


def test_the_deadline_is_stored_in_utc_with_its_offset(runtime: Runtime) -> None:
    """The TUI's naive local time was published without an offset: a phone in another
    timezone read it wrongly, and across a DST change it was an hour off (review of #243)."""
    local = datetime(2026, 10, 7, 18, 30)  # naive: this machine's local time
    runtime.set_auto_off(local)
    stored = runtime.remote_json()["auto_off_at"]
    assert isinstance(stored, str) and stored.endswith("+00:00")
    assert datetime.fromisoformat(stored) == local.astimezone(UTC)
    assert runtime.auto_off_deadline() == local.astimezone()


def test_extend_adds_an_hour_up_to_eight_hours_ahead(
    app: Any, runtime: Runtime, clock: Clock
) -> None:
    client = make_client(app)
    device_id = unlock(client, runtime).json()["device"]["id"]
    url = f"{base(runtime)}/api/remote/extend"
    runtime.set_auto_off(clock.now + timedelta(minutes=10))
    assert client.post(url, json={}).status_code == 403, "a write"
    runtime.set_allow_write(True)
    extended = client.post(url, json={})
    assert extended.status_code == 200
    assert extended.json() == {"auto_off_at": (clock.now + timedelta(minutes=70)).isoformat()}
    assert _audit_lines()[-1][1:] == [
        device_id,
        "remote/extend",
        f"extend auto_off_at={extended.json()['auto_off_at']}",
    ]
    for _ in range(10):
        client.post(url, json={})
    capped = client.get(f"{base(runtime)}/api/remote").json()["auto_off_at"]
    assert capped == (clock.now + timedelta(hours=8)).isoformat()


def test_extend_never_brings_a_far_deadline_closer(runtime: Runtime, clock: Clock) -> None:
    far = clock.now + timedelta(hours=10)  # serve --auto-off 600
    runtime.set_auto_off(far)
    assert runtime.extend_auto_off(clock.now) == far


def test_extend_without_a_deadline_is_409(app: Any, runtime: Runtime) -> None:
    client = make_client(app)
    unlock(client, runtime)
    runtime.set_allow_write(True)
    response = client.post(f"{base(runtime)}/api/remote/extend", json={})
    assert response.status_code == 409 and response.json()["error"] == "no_auto_off"


class FakeTimer:
    """``threading.Timer`` as the auto-off timer uses it, started by hand."""

    made: ClassVar[list[FakeTimer]] = []

    def __init__(self, delay: float, fire: Callable[[], None]) -> None:
        self.delay, self.fire = delay, fire
        self.daemon = False
        self.started = self.cancelled = False
        FakeTimer.made.append(self)

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True


def test_serves_auto_off_fires_at_the_deadline_and_rearms_after_an_extend(
    runtime: Runtime, clock: Clock
) -> None:
    FakeTimer.made = []
    off: list[str] = []
    timer = _AutoOffTimer(runtime, lambda: off.append("off"), timer=FakeTimer)
    runtime.set_auto_off(clock.now + timedelta(seconds=20))
    timer.auto_off_arm()
    (first,) = FakeTimer.made
    assert first.started and first.daemon and first.delay == 20
    runtime.extend_auto_off(clock.now)  # a phone: now an hour and 20 s away
    clock.advance(seconds=20)
    first.fire()
    assert off == [] and not timer.fired, "the deadline moved: wait for the new one"
    second = FakeTimer.made[-1]
    assert second is not first and second.delay == AUTO_OFF_CHECK_SECONDS
    clock.advance(hours=1)
    second.fire()
    assert off == ["off"] and timer.fired


def test_serves_auto_off_reads_the_wall_clock_every_30_s_so_a_sleep_cannot_hide_it(
    runtime: Runtime, clock: Clock
) -> None:
    """A timer counts the monotonic clock, which stands still while a laptop sleeps. Armed
    once for the whole hour, serve slept past its deadline and then sat half off for the
    hour it still owed: every request a 404, but no device revoked, no farewell, and the
    process up and pushing. It now waits 30 s at a time and reads the wall clock."""
    FakeTimer.made = []
    off: list[str] = []
    timer = _AutoOffTimer(runtime, lambda: off.append("off"), timer=FakeTimer)
    runtime.set_auto_off(clock.now + timedelta(hours=1))
    timer.auto_off_arm()
    (first,) = FakeTimer.made
    assert first.delay == AUTO_OFF_CHECK_SECONDS, "never the whole hour in one wait"
    clock.advance(seconds=30)
    first.fire()
    assert off == [] and FakeTimer.made[-1].delay == AUTO_OFF_CHECK_SECONDS
    clock.advance(hours=3)  # the lid was shut: the next 30 s of monotonic time come after it
    FakeTimer.made[-1].fire()
    assert off == ["off"] and timer.fired


def _free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def page(isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(remote_server, "_runtime", None)
    built = isolated_home.parent / "built"
    built.mkdir()
    (built / "index.html").write_text("<!doctype html>", encoding="utf-8")
    remote_server.install_page(built)
    return built


def test_serve_on_a_taken_port_fails_before_it_prints_anything(page: Path) -> None:
    """uvicorn's own bind turned a taken port into ``sys.exit(3)`` after the banner and
    the ``--json`` success payload (review of #243)."""
    import socket

    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        port = int(taken.getsockname()[1])
        ready: list[str] = []
        with pytest.raises(remote_server.RemoteBindError, match=f"cannot bind 127.0.0.1:{port}"):
            remote_server.run_foreground(port=port, ready=lambda: ready.append("banner"))
        assert ready == []
        result = CliRunner().invoke(cli, ["--json", "remote", "serve", "--port", str(port)])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"] == "remote_bind_failed"
    assert "url_local" not in result.stdout and "password" not in result.stdout


def test_the_link_serve_prints_takes_a_connection_before_uvicorn_runs(
    page: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The banner goes out once the port is bound, and whatever reads it may connect at
    once: a script reading the ``--json`` link, ngrok, a phone. The socket already
    listens then, so that connection waits for uvicorn instead of being refused."""
    import socket

    import uvicorn

    class NeverStarted:
        def __init__(self, config: Any) -> None:
            self.should_exit = False

        def run(self, sockets: Any = None) -> None:
            pass

    monkeypatch.setattr(uvicorn, "Server", NeverStarted)
    port = _free_port()
    reached: list[int] = []

    def banner() -> None:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as phone:
            reached.append(int(phone.getpeername()[1]))

    remote_server.run_foreground(port=port, ready=banner)
    assert reached == [port]


def test_a_remote_json_that_will_not_write_is_not_called_a_taken_port(
    page: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``serve`` answered every ``OSError`` out of the server as ``cannot bind
    127.0.0.1:<port>``, the deadline it could not write into ``remote.json`` included."""
    remote_server.runtime()  # the file is there: only serve's own write fails

    def unwritable(path: Path, **kwargs: object) -> object:
        raise PermissionError(errno.EACCES, "Permission denied", str(path))

    monkeypatch.setattr(remote_server, "replacement", unwritable)
    port = _free_port()
    result = CliRunner().invoke(cli, ["--json", "remote", "serve", "--port", str(port)])
    assert result.exit_code == 1
    answer = json.loads(result.stdout)
    assert answer["error"] == "remote_failed" and "Permission denied" in answer["detail"]


def test_serve_sets_the_deadline_notes_the_public_url_and_reports_auto_off(
    page: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import uvicorn

    seen: dict[str, object] = {}

    class Served:
        def __init__(self, config: Any) -> None:
            self.config = config
            self.should_exit = False

        def run(self, sockets: Any = None) -> None:
            state = remote_server.runtime()
            seen["deadline"] = state.auto_off_deadline()
            seen["origin"] = state.remote_public_origin()
            seen["proxy"] = (self.config.proxy_headers, self.config.forwarded_allow_ips)
            seen["ws_max_size"] = self.config.ws_max_size
            seen["bound"] = [sock.getsockname()[1] for sock in sockets]

    monkeypatch.setattr(uvicorn, "Server", Served)
    port = _free_port()
    before = datetime.now(UTC)
    result = CliRunner().invoke(
        cli,
        ["remote", "serve", "--port", str(port), "--auto-off", "5"],
        env={"AISQUARE_REMOTE_NGROK_URL": "abcd-12.ngrok-free.app"},
    )
    assert result.exit_code == 0, result.output
    deadline = seen["deadline"]
    assert isinstance(deadline, datetime)
    assert before + timedelta(minutes=4) < deadline <= datetime.now(UTC) + timedelta(minutes=5)
    assert seen["origin"] == "https://abcd-12.ngrok-free.app"
    assert seen["proxy"] == (True, "127.0.0.1") and seen["ws_max_size"] == 65_536
    assert seen["bound"] == [port]
    assert "auto-off: at" in result.stderr and "a phone can extend it" in result.stderr
    token = remote_server.runtime().token
    assert f"public link: https://abcd-12.ngrok-free.app/r/{token}/" in result.stderr
    assert remote_server.runtime().auto_off_deadline() is None, "no server, no deadline"
    never = CliRunner().invoke(cli, ["remote", "serve", "--port", str(port), "--auto-off", "0"])
    assert "auto-off: never (--auto-off 0)" in never.stderr


def test_serves_auto_off_stops_the_server_even_when_remote_json_cannot_be_written(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deadline is the server's to keep: a revoke that cannot be written must not leave
    ``serve`` running past it."""

    def unwritable(reason: str) -> None:
        raise OSError("remote.json: read-only file system")

    monkeypatch.setattr(remote_server, "revoke_every_remote_device", unwritable)
    server = SimpleNamespace(should_exit=False)
    with pytest.raises(OSError, match="read-only"):
        remote_server._remote_serve_off(runtime, server)
    assert server.should_exit is True


def test_serve_says_so_when_the_timer_ended_it(page: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(remote_server, "run_foreground", lambda *a, ready: ready() or True)
    result = CliRunner().invoke(cli, ["remote", "serve"])
    assert result.exit_code == 0
    assert "Remote turned off — the auto-off timer ran out" in result.stderr


def test_a_public_url_that_is_not_https_on_a_dns_name_is_refused(page: Path) -> None:
    for url in ("http://abcd-12.ngrok-free.app", "https://127.0.0.1", "https://u@x.example"):
        result = CliRunner().invoke(cli, ["--json", "remote", "serve", "--public-url", url])
        assert result.exit_code == 1 and json.loads(result.stdout)["error"] == "invalid_public_url"


# --- Remote off revokes every device, after the farewell ----------------------------------


def test_remote_off_says_farewell_then_revokes_every_device_with_4410(
    app: Any, runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(remote_server, "_runtime", runtime)
    farewells: list[tuple[list[str], str, list[str]]] = []
    monkeypatch.setattr(
        remote_push,
        "push_farewell",
        lambda ids, reason: farewells.append((list(ids), reason, runtime.device_ids())),
    )
    client = make_client(app)
    device_id = unlock(client, runtime).json()["device"]["id"]
    with client.websocket_connect(f"{base(runtime)}/ws") as ws:
        receive_within(ws)
        remote_server.revoke_every_remote_device("remote off")
        assert _closed_with(ws) == WS_CLOSE_REMOTE_OFF
    assert farewells == [([device_id], "remote off", [device_id])], "sent while they existed"
    assert runtime.device_rows() == []


def test_a_farewell_that_cannot_be_queued_still_turns_remote_off(
    app: Any, runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(remote_server, "_runtime", runtime)

    def broken(ids: object, reason: str) -> None:
        raise RuntimeError("no sender")

    monkeypatch.setattr(remote_push, "push_farewell", broken)
    unlock(make_client(app), runtime)
    remote_server.revoke_every_remote_device("remote off")
    assert runtime.device_rows() == []


# --- regenerate-password --new-link ------------------------------------------------------


def test_a_new_link_retires_the_old_one_on_the_running_server(
    app: Any, runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(app)
    unlock(client, runtime)
    old = base(runtime)
    monkeypatch.setattr(remote_server, "_runtime", None)  # the CLI is another process
    result = CliRunner().invoke(cli, ["--json", "remote", "regenerate-password", "--new-link"])
    assert result.exit_code == 0, result.output
    shown = json.loads(result.stdout)
    assert shown["url_local"] == f"http://127.0.0.1:8750/r/{shown['token']}/"
    assert client.get(f"{old}/api/remote").status_code == 404
    assert client.get(f"/r/{shown['token']}/api/remote").status_code == 401, "devices dropped"
    assert unlock(client, runtime, shown["password"]).status_code == 200
    assert runtime.token == shown["token"] and base(runtime) != old


# --- (7) the audit trail ------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["\n", "\r", "\x1b", "\u2028", "\u202e", "\x85", "\t"])
def test_no_field_of_an_audit_line_can_break_it_or_reorder_it(runtime: Runtime, bad: str) -> None:
    runtime.audit(f"dev{bad}x", f"note{bad}", f"note seq=1{bad}2026-01-01 forged line")
    (line,) = remote_audit_path().read_text(encoding="utf-8").splitlines()
    assert bad not in line
    assert line.split(" ", 3)[1:3] == ["dev?x", "note?"]


def test_audit_fields_are_capped(runtime: Runtime) -> None:
    runtime.audit("d" * 40, "e" * 40, "s" * 400)
    _ts, device, endpoint, summary = remote_audit_path().read_text(encoding="utf-8").split(" ", 3)
    assert (len(device), len(endpoint), len(summary.rstrip("\n"))) == (32, 32, 300)
    assert summary.rstrip("\n").endswith("…")
    assert _audit_clean("short", 10) == "short"


class FakeTeam:
    """``team_service``'s writes as the handlers call them, recorded."""

    def __init__(self) -> None:
        self.notes: list[dict[str, Any]] = []

    def add_note(self, text: str, **kwargs: Any) -> Any:
        self.notes.append({"text": text, **kwargs})
        envelope = SimpleNamespace(model_dump=lambda mode: {"seq": 7})
        return SimpleNamespace(kind=kwargs["kind"], seq=7, as_envelope=lambda: envelope)

    def claim_task(self, ref: str, *, session_ref: str | None) -> Any:
        return SimpleNamespace(id=ref, model_dump=lambda mode: {"id": ref})

    def finish_task(self, ref: str, *, note: str | None, session_ref: str | None) -> Any:
        return SimpleNamespace(id=ref, model_dump=lambda mode: {"id": ref})


@pytest.fixture
def team(monkeypatch: pytest.MonkeyPatch) -> FakeTeam:
    from aisquare.services import team as team_service

    fake = FakeTeam()
    for name in ("add_note", "claim_task", "finish_task"):
        monkeypatch.setattr(team_service, name, getattr(fake, name))
    return fake


def test_a_note_records_who_it_claims_to_be_from_and_who_it_is_for(team: FakeTeam) -> None:
    handlers = live_writes().handlers
    _result, summary = handlers["note"]({"text": "ship it", "to": "manager", "as": "coder-1"})
    assert summary == "note seq=7 to=manager as=coder-1"
    _result, plain = handlers["note"]({"text": "hello", "kind": "decision"})
    assert plain == "decision seq=7 to=- as=-"
    assert (
        handlers["task/claim"]({"ref": "tsk_1", "as": "coder-2"})[1] == "claimed tsk_1 as=coder-2"
    )
    assert handlers["task/done"]({"ref": "tsk_1"})[1] == "done tsk_1 as=-"


@pytest.mark.parametrize("kind", ["attention", "limited", "agent_exited", "switched", "note\nx"])
def test_a_phone_cannot_post_the_fleets_own_kinds(team: FakeTeam, kind: str) -> None:
    """A kind went to the board and the audit line as it came: a phone could forge the
    reports that wake the manager, and a newline in one forged an audit line."""
    with pytest.raises(RequestError) as refused:
        live_writes().handlers["note"]({"text": "x", "kind": kind})
    assert (refused.value.status, refused.value.error) == (400, "invalid")
    assert team.notes == []


def test_a_note_longer_than_the_cap_is_413(team: FakeTeam) -> None:
    with pytest.raises(RequestError) as refused:
        live_writes().handlers["note"]({"text": "x" * (NOTE_TEXT_MAX + 1)})
    assert (refused.value.status, refused.value.error) == (413, "too_large")
    live_writes().handlers["note"]({"text": "x" * NOTE_TEXT_MAX})
    assert len(team.notes) == 1


# --- (9) project/add stays inside the home directory --------------------------------------


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home directory of the test's own: ``Path.home()`` and ``~`` both point here."""
    home = (tmp_path / "home").resolve()
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


def _repo(path: Path) -> Path:
    (path / ".git").mkdir(parents=True)
    return path


def test_a_git_checkout_or_a_folder_of_repos_inside_home_is_a_project(home: Path) -> None:
    assert check_project_add_root(str(_repo(home / "code" / "app"))) == home / "code" / "app"
    assert check_project_add_root("~/code/app") == home / "code" / "app"
    _repo(home / "work" / "front")
    _repo(home / "work" / "back")
    assert check_project_add_root(str(home / "work")) == home / "work", "multi-repo parent"


@pytest.mark.parametrize(
    ("raw", "says"),
    [
        ("code/app", "not an absolute path"),
        ("", "required"),
        ("~/nowhere", "does not exist"),
        ("~/notes.txt", "not a directory"),
        ("~", "is your home directory"),
        ("~/.ssh", "hidden directory .ssh"),
        ("~/.config/thing", "hidden directory .config"),
        ("~/linked", "hidden directory .aisquare"),
        ("~/plain", "neither a git checkout nor a directory of repositories"),
    ],
)
def test_project_add_refuses_what_is_not_a_project_inside_home(
    home: Path, raw: str, says: str
) -> None:
    _repo(home / ".ssh")
    _repo(home / ".config" / "thing")
    _repo(home / ".aisquare" / "inner")
    (home / "notes.txt").write_text("x", encoding="utf-8")
    (home / "plain" / ".hg").mkdir(parents=True)  # a project root, but no repository in it
    with contextlib.suppress(OSError):  # a symlink into a hidden directory, judged where it goes
        (home / "linked").symlink_to(home / ".aisquare" / "inner")
    if raw == "~/linked" and not (home / "linked").is_symlink():
        pytest.skip("this platform cannot make the symlink")
    with pytest.raises(RequestError) as refused:
        check_project_add_root(raw)
    assert (refused.value.status, refused.value.error) == (400, "invalid")
    assert says in refused.value.message


def test_a_project_outside_home_is_refused(home: Path, tmp_path: Path) -> None:
    outside = _repo(tmp_path / "elsewhere")
    with pytest.raises(RequestError) as refused:
        check_project_add_root(str(outside))
    assert "outside your home directory" in refused.value.message


def test_a_path_over_the_cap_is_413(home: Path) -> None:
    with pytest.raises(RequestError) as refused:
        check_project_add_root("/" + "a" * 4_096)
    assert (refused.value.status, refused.value.error) == (413, "too_large")


def test_an_added_project_is_listed_where_the_phone_and_the_cli_look(
    home: Path, runtime: Runtime, tmp_path: Path
) -> None:
    """The add captured the directory as a hook does (``ensure_project``), and a capture is
    never listed: the phone was told ``added``, and its Projects screen, ``project list``
    and the sidebar stayed as they were (review of #243, round 2)."""
    root = _repo(home / "code" / "app")
    sources = remote_server.live_sources()
    app = build_app(runtime, sources=sources, writes=live_writes(), dist_dir=tmp_path)
    client = make_client(app)
    unlock(client, runtime)
    runtime.set_allow_write(True)
    first = client.post(f"{base(runtime)}/api/project/add", json={"path": "~/code/app"})
    again = client.post(f"{base(runtime)}/api/project/add", json={"path": str(root)})
    assert (first.status_code, again.status_code) == (200, 200), first.text
    assert (first.json()["added"], again.json()["added"]) == (True, False)
    assert first.json()["project"]["onboarded_at"] is not None
    listed = client.get(f"{base(runtime)}/api/projects").json()
    assert [row["root"] for row in listed] == [str(root)]
    assert [project.root for project in project_service.list_projects()] == [root]


@pytest.mark.parametrize("before", ["captured by a hook", "forgotten"])
def test_adding_a_directory_that_is_not_listed_lists_it(home: Path, before: str) -> None:
    root = _repo(home / "code" / "app")
    project = ProjectInfo(id=project_id_for(root), root=root, linked_repos=[])
    with store_session() as store:
        if before == "forgotten":
            store.onboard_project(project)
            store.forget_project(project.id)
        else:
            store.ensure_project(project)
    assert project_service.list_projects() == []
    answer, summary = live_writes().handlers["project/add"]({"path": str(root)})
    assert answer["added"] is True
    assert [listed.root for listed in project_service.list_projects()] == [root]
    assert summary == f"added {project.id} {root}"


# --- (8) the Origin of a write or a socket ------------------------------------------------


def test_a_write_without_an_origin_or_from_another_is_403(app: Any, runtime: Runtime) -> None:
    client = make_client(app)
    unlock(client, runtime)
    runtime.set_allow_write(True)
    url = f"{base(runtime)}/api/note"
    for origin in (None, "https://evil.example", "null", "http://testserver.evil.example"):
        headers = {"origin": origin} if origin else {}
        bare = TestClient(app, cookies=dict(client.cookies), headers=headers)
        response = bare.post(url, json={"text": "x"})
        assert response.status_code == 403, origin
        assert response.json() == {
            "error": "bad_origin",
            "message": "this request did not come from the remote page",
        }
    assert client.post(url, json={"text": "x"}).status_code == 200


def test_x_forwarded_host_is_never_what_the_origin_is_checked_against(
    app: Any, runtime: Runtime
) -> None:
    client = make_client(app)
    unlock(client, runtime)
    runtime.set_allow_write(True)
    forged = {"origin": "https://evil.example", "x-forwarded-host": "evil.example"}
    response = client.post(f"{base(runtime)}/api/note", json={"text": "x"}, headers=forged)
    assert response.status_code == 403


def test_the_origin_of_an_https_hop_is_https(runtime: Runtime, tmp_path: Path) -> None:
    app = build_app(runtime, sources=_sources(), dist_dir=tmp_path)
    secure = make_client(app, base_url="https://testserver")
    assert unlock(secure, runtime).status_code == 200
    plain_origin = make_client(
        app, base_url="https://testserver", headers={"origin": "http://testserver"}
    )
    assert unlock(plain_origin, runtime).status_code == 403


def test_a_get_needs_no_origin(app: Any, runtime: Runtime) -> None:
    client = make_client(app)
    unlock(client, runtime)
    bare = TestClient(app, cookies=dict(client.cookies))
    assert bare.get(f"{base(runtime)}/api/board").status_code == 200
    assert bare.get(f"{base(runtime)}/").status_code != 403


def test_a_socket_from_another_origin_is_denied_403(app: Any, runtime: Runtime) -> None:
    client = make_client(app)
    unlock(client, runtime)
    with (
        pytest.raises(WebSocketDenialResponse) as denied,
        client.websocket_connect(f"{base(runtime)}/ws", headers={"origin": "https://evil.example"}),
    ):
        pass
    assert denied.value.status_code == 403


@pytest.mark.parametrize(
    ("scheme", "host", "allowed"),
    [
        ("http", "127.0.0.1:8750", "http://127.0.0.1:8750"),
        ("https", "Abcd-12.Ngrok-Free.App", "https://abcd-12.ngrok-free.app"),
        ("wss", "abcd-12.ngrok-free.app", "https://abcd-12.ngrok-free.app"),
        ("ws", "localhost:8750", "http://localhost:8750"),
    ],
)
def test_the_allowed_origin_is_the_requests_own_scheme_and_host(
    scheme: str, host: str, allowed: str
) -> None:
    scope = {"scheme": scheme, "headers": [(b"host", host.encode())]}
    assert allowed_origin(scope) == allowed


# --- remote.json: one writer at a time, and nothing rewritten for nothing -----------------


def test_reading_remote_json_does_not_rewrite_it(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``asq remote status`` rewrote the file from its own snapshot and could drop a device
    that had just unlocked; a v2 file that parses is now only read (review of #243)."""
    from aisquare.core.atomic import replacement

    written: list[Path] = []

    def spy(path: Path, **kwargs: Any) -> Any:
        written.append(path)
        return replacement(path, **kwargs)

    monkeypatch.setattr(remote_server, "replacement", spy)
    before = remote_state_path().read_bytes()
    for _ in range(3):
        Runtime(remote_state_path(), remote_audit_path())
    assert written == [] and remote_state_path().read_bytes() == before


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="POSIX permissions, which root ignores",
)
def test_a_file_that_cannot_be_read_is_never_replaced(runtime: Runtime) -> None:
    """``_load`` minted a new token and password on ANY OSError, killing the phone's link."""
    path = remote_state_path()
    before = path.read_bytes()
    path.chmod(0)
    try:
        with pytest.raises(remote_server.RemoteError, match="could not be read"):
            Runtime(path, remote_audit_path())
    finally:
        path.chmod(0o600)
    assert path.read_bytes() == before


def test_a_read_inside_another_processs_rename_on_windows_is_tried_again(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On NTFS, opening a file while another process renames over it is refused for the
    rename's width (``paths.despite_windows_contention``). Read once, that refusal failed a
    CLI command, or the TUI's first read, with a sentence about permissions."""
    path = remote_state_path()
    read_bytes = Path.read_bytes
    refused: list[Path] = []

    def busy_once(self: Path) -> bytes:
        if self == path and not refused:
            refused.append(self)
            raise PermissionError(errno.EACCES, "Access is denied", str(self))
        return read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", busy_once)
    monkeypatch.setattr(sys, "platform", "win32")
    assert Runtime(path, remote_audit_path()).token == runtime.token
    assert refused == [path]


def test_a_hand_edit_that_breaks_the_json_costs_no_phone_its_link(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One deleted closing brace, then a plain ``asq remote status``: that read minted a new
    link and passphrase and turned writes off, and the running server adopted them on its
    next request, so every phone's link was a 404 (review of #243, round 2)."""
    monkeypatch.setattr(remote_server, "_runtime", None)  # the CLI is another process
    runtime.set_allow_write(True)
    path = remote_state_path()
    broken = path.read_bytes().rstrip()[:-1]
    path.write_bytes(broken)
    result = CliRunner().invoke(cli, ["--json", "remote", "status"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"] == "remote_state_unreadable"
    assert path.read_bytes() == broken
    assert runtime.reload_if_changed() is False, "the server keeps what it has"
    assert (runtime.password, runtime.allow_write) == (PASSWORD, True)


@pytest.mark.parametrize(
    "command",
    [
        ["status"],
        ["allow-write", "on"],
        ["regenerate-password"],
        ["revoke", "--all"],
        ["revoke", "dev_00000000"],
    ],
    ids=" ".join,
)
def test_every_command_says_why_remote_json_cannot_be_used(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, command: list[str]
) -> None:
    """``allow-write`` went to the file unchecked, and answered a file it could not use with a
    traceback and no JSON at all; the others answered without saying why."""
    monkeypatch.setattr(remote_server, "_runtime", None)  # the CLI is another process
    path = remote_state_path()
    path.write_bytes(b'{"version": 2,')
    result = CliRunner().invoke(cli, ["--json", "remote", *command])
    assert result.exit_code == 1
    answer = json.loads(result.stdout)
    assert answer["error"] == "remote_state_unreadable"
    assert f"{path} is not a JSON object" in answer["detail"]
    assert path.read_bytes() == b'{"version": 2,'


@pytest.mark.parametrize(
    "command",
    [
        ["allow-write", "on"],
        ["regenerate-password"],
        ["regenerate-password", "--new-link"],
        ["revoke", "--all"],
        ["revoke", "<the device>"],
    ],
    ids=" ".join,
)
def test_every_command_says_when_remote_json_cannot_be_written(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, command: list[str]
) -> None:
    """A ``remote.json`` the commands could read but not replace (a read-only or full home)
    ended every one that writes in a hundred-line traceback, with nothing on stdout under
    ``--json`` (sweep of #243): the file unreadable was already a clean answer, the file
    unwritable was not."""
    unlocked = runtime.unlock_device(PASSWORD, "Pixel")
    assert unlocked is not None
    command = [unlocked[1].id if part == "<the device>" else part for part in command]
    monkeypatch.setattr(remote_server, "_runtime", None)  # the CLI is another process
    path = remote_state_path()
    before = path.read_bytes()

    def unwritable(target: Path, **kwargs: object) -> object:
        raise PermissionError(errno.EACCES, "Permission denied", str(target))

    monkeypatch.setattr(remote_server, "replacement", unwritable)
    result = CliRunner().invoke(cli, ["--json", "remote", *command])
    assert result.exit_code == 1, result.output
    answer = json.loads(result.stdout)
    assert answer["error"] == "remote_state_unwritable"
    assert "Permission denied" in answer["detail"]
    assert path.read_bytes() == before, "nothing was changed"
    monkeypatch.setattr(remote_server, "_runtime", None)  # another process again
    human = CliRunner().invoke(cli, ["remote", *command])
    assert human.exit_code == 1
    assert isinstance(human.exception, SystemExit), "a sentence, never a traceback"
    assert f"{path} could not be written" in human.stderr


@pytest.mark.parametrize("command", [["status"], ["serve"]], ids=" ".join)
def test_a_home_that_will_not_take_a_first_remote_json_is_a_sentence(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, command: list[str]
) -> None:
    """``status`` and ``serve`` make ``remote.json`` the first time; in a home that refused
    it they ended in a traceback, before anything else was said."""
    monkeypatch.setattr(remote_server, "_runtime", None)

    def unwritable(target: Path, **kwargs: object) -> object:
        raise PermissionError(errno.EACCES, "Permission denied", str(target))

    monkeypatch.setattr(remote_server, "replacement", unwritable)
    result = CliRunner().invoke(cli, ["--json", "remote", *command])
    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["error"] == "remote_state_unwritable"
    assert not remote_state_path().exists()


def test_a_page_that_cannot_be_installed_is_a_sentence(
    isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``install-page`` into a directory it could not write ended in a traceback, and a
    ``--json`` caller got nothing."""
    import shutil

    built = tmp_path / "dist"
    built.mkdir()
    (built / "index.html").write_text("<!doctype html>", encoding="utf-8")

    def refused(source: object, target: object, *args: object, **kwargs: object) -> object:
        raise PermissionError(errno.EACCES, "Permission denied", str(target))

    monkeypatch.setattr(shutil, "copytree", refused)
    result = CliRunner().invoke(cli, ["--json", "remote", "install-page", str(built)])
    assert result.exit_code == 1, result.output
    answer = json.loads(result.stdout)
    assert answer["error"] == "remote_page_unwritable" and "Permission denied" in answer["detail"]


def test_a_write_waits_for_another_process_holding_the_lock(runtime: Runtime) -> None:
    """A CLI revoke landing inside a server's flush was undone by it: every read-modify-write
    now holds ``remote.json.lock``, and starts from what is on disk once it has it."""
    from aisquare.core.locking import lock_exclusive
    from aisquare.core.locking import unlock as release

    lock_path = remote_state_path().with_name("remote.json.lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    lock_exclusive(fd)
    shell = Runtime(remote_state_path(), remote_audit_path())
    done = threading.Event()

    def toggle() -> None:
        shell.set_allow_write(True)
        done.set()

    toggler = threading.Thread(target=toggle)
    try:
        toggler.start()
        time.sleep(0.3)
        assert not done.is_set(), "it waited for the lock"
        assert json.loads(remote_state_path().read_bytes())["allow_write"] is False
    finally:
        release(fd)
        os.close(fd)
    toggler.join(5)
    assert done.is_set() and runtime.allow_write is True


def test_a_slow_restriction_of_the_temp_holds_no_lock_and_loses_no_revoke(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The flush made and restricted its owner-only temp inside ``remote.json.lock``. On
    Windows that is ``icacls``, seconds under an antivirus scan: a CLI ``revoke`` gave up
    waiting after 2 s and wrote without the lock, then the flush's rename landed after it
    and brought the device back."""
    from aisquare.core import paths

    unlocked = runtime.unlock_device(PASSWORD, "Pixel")
    assert unlocked is not None
    device_id = unlocked[1].id
    restricting, done = threading.Event(), threading.Event()
    real = paths.restrict_to_owner

    def icacls_under_a_scan(path: Path) -> bool:
        if threading.current_thread().name == "asq-test-flush":
            restricting.set()
            assert done.wait(10), "the test never let the restriction finish"
        return real(path)

    monkeypatch.setattr(paths, "restrict_to_owner", icacls_under_a_scan)
    flush = threading.Thread(target=runtime.flush_last_seen, name="asq-test-flush")
    flush.start()
    try:
        assert restricting.wait(5)
        shell = Runtime(remote_state_path(), remote_audit_path())  # the CLI: a lock of its own
        with caplog.at_level("WARNING", logger=remote_server.__name__):
            assert shell.revoke_device(device_id)
        assert "not taken" not in caplog.text, "the revoke had to write without the lock"
    finally:
        done.set()
        flush.join(10)
    devices = json.loads(remote_state_path().read_bytes())["devices"]
    assert device_id not in [device["id"] for device in devices]
    assert not runtime.device_is_live(device_id)


def test_a_slow_write_of_remote_json_holds_up_no_read(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every request takes the runtime's lock (the gate reads the state under it), and the
    write's fsyncs and rename ran inside it: a write on a busy disk held up every request
    and every socket tick for as long as the disk took."""
    from aisquare.core.atomic import Replacement

    publishing, done = threading.Event(), threading.Event()
    real = Replacement.publish

    def busy_disk(self: Replacement, body: str | bytes) -> None:
        publishing.set()
        assert done.wait(10), "the test never let the write finish"
        real(self, body)

    monkeypatch.setattr(Replacement, "publish", busy_disk)
    writer = threading.Thread(target=runtime.set_allow_write, args=(True,))
    writer.start()
    try:
        assert publishing.wait(5)
        read: list[dict[str, object]] = []
        reader = threading.Thread(target=lambda: read.append(runtime.remote_json()))
        reader.start()
        reader.join(2)
        assert read, "the read waited for the disk"
        assert read[0]["allow_write"] is True, "what was decided, ahead of the rename"
    finally:
        done.set()
        writer.join(10)
    assert json.loads(remote_state_path().read_bytes())["allow_write"] is True
    assert runtime.reads == 0, "its own write is never read back as another process's"


def test_a_v1_file_written_under_a_running_server_is_not_adopted(runtime: Runtime) -> None:
    """An older build that rewrites the file in the old shape would hand the server raw
    session cookies to trust; the server keeps what it has and writes v2 back."""
    path = remote_state_path()
    raw = json.loads(path.read_bytes())
    path.write_text(json.dumps({"token": raw["token"], "password": "x", "sessions": []}))
    assert runtime.reload_if_changed() is False
    assert runtime.password == PASSWORD
    runtime.flush_last_seen()
    assert json.loads(path.read_bytes())["version"] == 2
