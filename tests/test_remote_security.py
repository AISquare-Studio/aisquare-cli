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
    NOTE_TO_MAX,
    REMOTE_KEY_NAME,
    SEND_KEYS_KEYS_MAX,
    SEND_KEYS_TEXT_MAX,
    UNLOCK_GLOBAL_FAILURES,
    UNLOCK_LIMIT,
    UNLOCK_WINDOW_SECONDS,
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
from tests.cli_tree import root_command
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
        return SimpleNamespace(dead=False, current_command="claude", server_started=self.STARTED)

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
        ("\r", "the pad's Enter key"),
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


def test_a_newline_is_still_text(pane: FakePane) -> None:
    send = live_writes().handlers["send-keys"]
    send({"agent": "coder-1", "text": "a b\nc"})
    assert pane.sent == [("literal", "a b\nc")]


def test_a_tab_typed_is_the_tab_key_and_is_refused(pane: FakePane) -> None:
    """``"\\t"`` is the Tab key's own byte, and Claude Code's prompt takes it as the key (an
    open suggestion accepted), never as a tab of the message: a table row pasted into the
    input bar arrived as other text than was sent (review of #243, round 5)."""
    send = live_writes().handlers["send-keys"]
    with pytest.raises(RequestError) as refused:
        send({"agent": "coder-1", "text": "name\tvalue", "enter": True})
    assert (refused.value.status, refused.value.error) == (400, "invalid")
    assert refused.value.message == (
        "'text' holds the control character U+0009 — send the pad's Tab key instead"
    )
    assert pane.sent == []


@pytest.mark.parametrize("text", ["\r", "first line\r\nsecond line"], ids=repr)
def test_a_carriage_return_typed_is_the_enter_key_and_is_refused(pane: FakePane, text: str) -> None:
    """``"\\r"`` is the Enter key's own byte: typed, it took a dialog's highlighted option
    while the audit line said ``enter=False``, and each line of a CRLF text went in as a
    prompt of its own (review of #243, round 3). ``enter`` and the pad's Enter key say
    so on the trail."""
    send = live_writes().handlers["send-keys"]
    with pytest.raises(RequestError) as refused:
        send({"agent": "coder-1", "text": text, "enter": False})
    assert (refused.value.status, refused.value.error) == (400, "invalid")
    assert refused.value.message == (
        "'text' holds the control character U+000D — send the pad's Enter key instead"
    )
    assert pane.sent == []


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
    rule = (UNLOCK_LIMIT, UNLOCK_WINDOW_SECONDS)
    for n in range(500):
        assert limiter.limiter_retry_after(f"198.51.{n // 250}.{n % 250}", *rule) is None
    now[0] += 61
    assert limiter.limiter_retry_after("203.0.113.5", *rule) is None
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


class FullDisk:
    """``remote.json`` that will not write, as on a full disk, until :meth:`fixed`: its temp
    is made, and writing the bytes into it fails."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from aisquare.core.atomic import Replacement

        self.full = True
        real = Replacement.publish

        def publish(replacement: Replacement, body: str | bytes) -> None:
            if self.full:
                raise OSError(errno.ENOSPC, "No space left on device")
            real(replacement, body)

        monkeypatch.setattr(Replacement, "publish", publish)

    def fixed(self) -> None:
        self.full = False


def _devices_on_disk() -> list[str]:
    return [row["id"] for row in json.loads(remote_state_path().read_bytes())["devices"]]


def test_an_unlock_that_cannot_be_saved_adds_no_device_and_says_why(
    app: Any, runtime: Runtime, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    """The device was added in memory before its write, and the write's error answered a
    bare 500 with no cookie: each try left a device the R panel, ``status`` and every
    Devices screen listed as signed in, whose secret no browser held, and the next flush
    saved them all (sweep 2 of #243)."""
    disk = FullDisk(monkeypatch)
    phone = make_client(app)
    for _ in range(3):
        refused = unlock(phone, runtime)
        assert (refused.status_code, refused.json()["error"]) == (503, "remote_state_unwritable")
        assert "set-cookie" not in refused.headers
        assert str(isolated_home) not in refused.text, "no path for a phone not unlocked yet"
    assert runtime.device_rows() == []
    disk.fixed()
    runtime.flush_last_seen()
    assert _devices_on_disk() == [], "no phantom saved later either"
    assert unlock(phone, runtime).status_code == 200
    assert len(runtime.device_rows()) == 1


def test_a_reactivation_that_cannot_be_saved_leaves_the_device_its_old_cookie(
    app: Any, runtime: Runtime, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The new secret went into memory before the write: once that failed, the device held a
    digest nobody had, and the phone's old cookie no longer named it, so its next unlock made
    a second device and the stale one was saved (sweep 2 of #243)."""
    phone = _from(app, "198.51.100.70")
    device_id = unlock(phone, runtime).json()["device"]["id"]
    old_secret = phone.cookies[COOKIE]
    clock.advance(hours=25)  # idle: signed out, and a known device
    disk = FullDisk(monkeypatch)
    refused = unlock(phone, runtime)
    assert (refused.status_code, refused.json()["error"]) == (503, "remote_state_unwritable")
    known = runtime.known_device_for_cookie(old_secret)
    assert known is not None and known.id == device_id, "the old cookie still names it"
    disk.fixed()
    again = unlock(phone, runtime)
    assert again.status_code == 200 and again.json()["device"]["id"] == device_id
    assert [row["id"] for row in runtime.device_rows()] == [device_id]


def test_a_wrong_guess_on_a_full_disk_is_still_a_wrong_guess_and_still_counted(
    app: Any, runtime: Runtime, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Counting it wrote ``remote.json``, and the write's error made a typo a bare 500 that
    read as the machine's fault (sweep 2 of #243). It is counted in memory, and logged."""
    phone = _from(app, "198.51.100.71")
    unlock(phone, runtime)
    FullDisk(monkeypatch)
    with caplog.at_level("WARNING", logger=remote_server.__name__):
        stranger = _from(app, "198.51.100.72")
        wrong = unlock(stranger, runtime, "wrong")
        assert (wrong.status_code, wrong.json()["error"]) == (401, "wrong_password")
        guesser = _from(app, "198.51.100.73")
        guesser.cookies.set(COOKIE, phone.cookies[COOKIE])
        assert unlock(guesser, runtime, "wrong").status_code == 401
    assert UnlockBudget(runtime).budget_failures() == 1
    assert runtime._state.devices[0].failed_unlocks == 1
    assert "counted in memory only" in caplog.text


def test_an_unlock_whose_audit_line_cannot_be_written_still_hands_over_its_cookie(
    app: Any, runtime: Runtime, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The device was saved, then the audit line raised: a bare 500, no cookie, and a device
    on disk, signed in, that no browser could use (sweep 2 of #243). The line is best
    effort for an unlock: what it records is saved already, and the log says it is missing."""

    def unwritable(self: Runtime, device_id: str, endpoint: str, summary: str) -> None:
        raise PermissionError(errno.EACCES, "Permission denied", str(remote_audit_path()))

    monkeypatch.setattr(Runtime, "audit", unwritable)
    phone = make_client(app)
    with caplog.at_level("WARNING", logger=remote_server.__name__):
        response = unlock(phone, runtime)
    assert response.status_code == 200 and COOKIE in response.headers["set-cookie"]
    assert phone.get(f"{base(runtime)}/api/devices").status_code == 200
    assert "audit line could not be written" in caplog.text


def test_an_extend_that_cannot_be_saved_moves_no_deadline_and_says_why(
    app: Any, runtime: Runtime, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The later deadline went into memory before its write: once that failed, the gate and
    the panel kept Remote public another hour while the phone was told the extend failed,
    as a bare 500 (sweep 2 of #243, the unlock's class)."""
    client = make_client(app)
    unlock(client, runtime)
    runtime.set_allow_write(True)
    deadline = clock.now + timedelta(minutes=10)
    runtime.set_auto_off(deadline)
    FullDisk(monkeypatch)
    response = client.post(f"{base(runtime)}/api/remote/extend", json={})
    assert (response.status_code, response.json()["error"]) == (503, "remote_state_unwritable")
    assert runtime.auto_off_deadline() == deadline


def test_a_revoke_that_cannot_be_saved_holds_here_and_says_it_was_not_saved(
    app: Any, runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A revoke holds in memory, where the gate reads it (review of #243, round 2), but the
    write's error answered a bare 500 that said nothing of it (sweep 2 of #243). It took
    effect, so the audit trail records it, as not saved: it had no line at all (review of
    #243, round 4)."""
    mine, theirs = make_client(app), make_client(app)
    unlock(mine, runtime)
    other = unlock(theirs, runtime).json()["device"]["id"]
    runtime.set_allow_write(True)
    FullDisk(monkeypatch)
    response = mine.delete(f"{base(runtime)}/api/devices/{other}")
    assert (response.status_code, response.json()["error"]) == (503, "remote_state_unwritable")
    assert "revoked on the running Remote" in response.json()["message"]
    assert f"run  aisquare remote revoke {other}  on the machine" in response.json()["message"]
    assert other not in runtime.device_ids()
    assert theirs.get(f"{base(runtime)}/api/board").status_code == 401
    assert _audit_lines()[-1][2:] == ["devices/revoke", f"{other} unsaved"]


def test_a_revoke_that_can_be_neither_saved_nor_audited_still_says_it_was_not_saved(
    app: Any, runtime: Runtime, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The audit line of a revoke that could not be saved is best effort, as an unlock's is:
    a home that refuses ``remote.json`` may refuse its audit log too, and that must not turn
    the answer into a bare 500."""
    mine = make_client(app)
    device_id = unlock(mine, runtime).json()["device"]["id"]
    FullDisk(monkeypatch)

    def unwritable(self: Runtime, device_id: str, endpoint: str, summary: str) -> None:
        raise PermissionError(errno.EACCES, "Permission denied", str(remote_audit_path()))

    monkeypatch.setattr(Runtime, "audit", unwritable)
    with caplog.at_level("WARNING", logger=remote_server.__name__):
        response = mine.delete(f"{base(runtime)}/api/devices/{device_id}")
    assert (response.status_code, response.json()["error"]) == (503, "remote_state_unwritable")
    assert device_id not in runtime.device_ids()
    assert "devices/revoke audit line could not be written" in caplog.text


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


def test_revoking_another_device_asks_the_gates_again_in_the_thread_that_revokes(
    app: Any, runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Writes were on when the request arrived, and off by the time a thread of the shared
    pool revoked: as a queued write did, the revoke went ahead, the owner's own phone
    signed out by a device whose writes were just switched off (sweep 2 of #243)."""
    mine, theirs = make_client(app), make_client(app)
    unlock(mine, runtime)
    other = unlock(theirs, runtime).json()["device"]["id"]
    monkeypatch.setattr(app.kit, "kit_write_allowed", lambda: True)  # on when it arrived
    response = mine.delete(f"{base(runtime)}/api/devices/{other}")
    assert (response.status_code, response.json()["error"]) == (403, "read_only")
    assert other in runtime.device_ids()
    assert [line for line in _audit_lines() if line[2] == "devices/revoke"] == []


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


def test_an_extend_asks_the_gates_again_in_the_thread_that_extends(
    app: Any, runtime: Runtime, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The switch is read when the request arrives, and the extend runs later, in a thread
    of the pool every read and socket snapshot shares: as a write queued for its thread
    did, an extend whose writes were switched off meanwhile still gave Remote another
    public hour (sweep 2 of #243)."""
    client = make_client(app)
    unlock(client, runtime)
    deadline = clock.now + timedelta(minutes=10)
    runtime.set_auto_off(deadline)
    monkeypatch.setattr(app.kit, "kit_write_allowed", lambda: True)  # on when it arrived
    response = client.post(f"{base(runtime)}/api/remote/extend", json={})
    assert (response.status_code, response.json()["error"]) == (403, "read_only")
    assert runtime.auto_off_deadline() == deadline
    assert [line for line in _audit_lines() if line[2] == "remote/extend"] == []


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


def test_serves_auto_off_turns_off_once_when_its_check_and_the_way_out_both_find_it_past(
    runtime: Runtime, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancel cannot stop a check already running, and ``serve``'s way out fires the
    auto-off itself once the deadline has passed and the timer has not fired. A Ctrl-C
    while the 30 s check was between reading the deadline and saying it fired turned
    Remote off from both threads: two farewells, two revokes."""
    FakeTimer.made = []
    off: list[str] = []
    timer = _AutoOffTimer(runtime, lambda: off.append("off"), timer=FakeTimer)
    runtime.set_auto_off(clock.now - timedelta(seconds=1))
    read, way_out_done = threading.Event(), threading.Event()
    deadline_of = runtime.auto_off_deadline

    def slow_read() -> datetime | None:  # the check's thread waits on the way out once read
        deadline = deadline_of()
        if threading.current_thread() is not threading.main_thread():
            read.set()
            way_out_done.wait(5)
        return deadline

    monkeypatch.setattr(runtime, "auto_off_deadline", slow_read)
    check = threading.Thread(target=timer.auto_off_fire, daemon=True)
    check.start()
    assert read.wait(5)
    timer.auto_off_cancel()  # serve's way out, as run_foreground does it
    timer.auto_off_fire()
    way_out_done.set()
    check.join(5)
    assert not check.is_alive()
    assert off == ["off"] and timer.fired


def test_a_check_running_when_serve_cancels_its_auto_off_arms_no_other(
    runtime: Runtime, clock: Clock
) -> None:
    """``serve``'s way out cancels the timer, but a check already past its wait found the
    deadline still ahead and armed the next one, which nothing would cancel: the checks
    went on after ``serve`` had returned, for as long as the process lived."""
    FakeTimer.made = []
    off: list[str] = []
    timer = _AutoOffTimer(runtime, lambda: off.append("off"), timer=FakeTimer)
    runtime.set_auto_off(clock.now + timedelta(minutes=5))
    timer.auto_off_arm()
    (check,) = FakeTimer.made
    timer.auto_off_cancel()  # as the check began: a Timer's cancel cannot stop it now
    check.fire()
    assert FakeTimer.made == [check] and check.cancelled
    assert off == [] and not timer.fired


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
    assert "auto-off: at" in result.stderr
    assert "no phone can extend it while writes are off" in result.stderr
    token = remote_server.runtime().token
    assert f"public link: https://abcd-12.ngrok-free.app/r/{token}/" in result.stderr
    assert remote_server.runtime().auto_off_deadline() is None, "no server, no deadline"
    never = CliRunner().invoke(cli, ["remote", "serve", "--port", str(port), "--auto-off", "0"])
    assert "auto-off: never (--auto-off 0)" in never.stderr


@pytest.mark.parametrize(
    ("writes", "gate", "extend"),
    [
        (True, "ON — writes are audited", "a phone can extend it"),
        (False, "off (read-only)", "no phone can extend it while writes are off"),
    ],
    ids=["writes-on", "writes-off"],
)
def test_serves_banner_offers_the_extension_only_while_writes_are_on(
    page: Path, monkeypatch: pytest.MonkeyPatch, writes: bool, gate: str, extend: str
) -> None:
    """Extending auto-off is a write: with writes off, the default, the page's Extend is
    greyed out and ``api/remote/extend`` answers 403 ``read_only``. The banner said a phone
    could extend it all the same, on the line under "write actions: off" (review of #243,
    round 3, 12/13)."""
    import uvicorn

    class Served:
        def __init__(self, config: Any) -> None:
            self.should_exit = False

        def run(self, sockets: Any = None) -> None:
            pass

    monkeypatch.setattr(uvicorn, "Server", Served)
    remote_server.runtime().set_allow_write(writes)
    result = CliRunner().invoke(
        cli, ["remote", "serve", "--port", str(_free_port()), "--auto-off", "5"]
    )
    assert result.exit_code == 0, result.output
    lines = result.stderr.splitlines()
    assert next(line for line in lines if line.startswith("write actions: ")).startswith(
        f"write actions: {gate}   · "
    )
    auto_off = next(line for line in lines if line.startswith("auto-off: at "))
    assert auto_off.endswith(" (in 5 min) · " + extend), auto_off


def test_serves_auto_off_help_offers_the_extension_only_while_writes_are_on() -> None:
    remote: Any = root_command().commands["remote"]
    (auto_off,) = [param for param in remote.commands["serve"].params if "--auto-off" in param.opts]
    assert "a phone can extend it while writes are on" in auto_off.help


def test_serves_auto_off_stops_the_server_even_when_remote_json_cannot_be_written(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deadline is the server's to keep: a revoke that cannot be written must not leave
    ``serve`` running past it. Nor may it raise on the timer's thread, skipping the
    deadline's clearing: what could not be done comes back, for the way out to say (sweep
    2 of #243)."""

    def unwritable(reason: str) -> None:
        raise OSError("remote.json: read-only file system")

    cleared: list[object] = []
    monkeypatch.setattr(remote_server, "revoke_every_remote_device", unwritable)
    monkeypatch.setattr(runtime, "set_auto_off", cleared.append)
    server = SimpleNamespace(should_exit=False)
    failure = remote_server._remote_serve_off(runtime, server)
    assert server.should_exit is True
    assert cleared == [None], "the deadline is still cleared"
    assert failure is not None and "could not be revoked (remote.json: read-only" in failure
    assert "aisquare remote revoke --all" in failure
    monkeypatch.setattr(remote_server, "revoke_every_remote_device", lambda reason: None)
    assert remote_server._remote_serve_off(runtime, server) is None, "all of it done"


def test_serves_auto_off_on_a_full_disk_says_the_phones_were_not_signed_out(
    page: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Past the deadline on a home that would not write, ``serve`` printed a thread's
    traceback, then "Remote turned off" and exit 0, under ``--json`` too, and the devices
    stayed in ``remote.json``: the next Remote accepted their cookies with no passphrase
    (sweep 2 of #243). The way out's own write, failing the same way, is one line too."""
    import uvicorn

    class StopsWhenTold:
        def __init__(self, config: Any) -> None:
            self.should_exit = False

        def run(self, sockets: Any = None) -> None:
            deadline = time.monotonic() + 10
            while not self.should_exit and time.monotonic() < deadline:
                time.sleep(0.01)

    state = remote_server.runtime()
    state._state.password = PASSWORD
    state._save_state()
    unlocked = state.unlock_device(PASSWORD, "Pixel")
    assert unlocked is not None
    now = [datetime.now(UTC)]
    monkeypatch.setattr(remote_server, "_remote_now", lambda: now[0])
    monkeypatch.setattr(remote_server, "AUTO_OFF_CHECK_SECONDS", 0.05)
    monkeypatch.setattr(uvicorn, "Server", StopsWhenTold)
    disk = FullDisk(monkeypatch)
    disk.fixed()  # serve writes its deadline before the disk fills up

    def banner() -> None:
        now[0] += timedelta(minutes=2)  # past the 1-minute deadline
        disk.full = True

    with (
        caplog.at_level("WARNING", logger=remote_server.__name__),
        pytest.raises(remote_server.RemoteOffIncomplete, match="could not be revoked") as off,
    ):
        remote_server.run_foreground(port=_free_port(), auto_off_minutes=1, ready=banner)
    assert str(off.value).startswith("Remote turned off — the auto-off timer ran out, but")
    assert "Traceback" not in capfd.readouterr().err
    assert _devices_on_disk() == [unlocked[1].id], "what the phone must be told to revoke"
    out = [r for r in caplog.records if "on the way out failed" in r.getMessage()]
    assert len(out) == 1 and out[0].exc_info is None, out
    assert "No space left on device" in out[0].getMessage()


@pytest.mark.parametrize("past", [True, False], ids=["past the deadline", "before it"])
def test_a_ctrl_c_of_serve_once_its_auto_off_time_came_is_auto_off(
    page: Path, monkeypatch: pytest.MonkeyPatch, past: bool
) -> None:
    """``serve``'s timer counts the monotonic clock, so a machine that slept past the deadline
    checks it up to half a minute after waking. A Ctrl-C then cleared the deadline and
    signed no phone out, where one a moment later found them all signed out by auto-off.
    Before the deadline, a Ctrl-C still revokes nothing (SPEC §2.4)."""
    import uvicorn

    class CtrlC:
        def __init__(self, config: Any) -> None:
            self.should_exit = False

        def run(self, sockets: Any = None) -> None:
            raise KeyboardInterrupt  # uvicorn re-raises the Ctrl-C it caught, once stopped

    state = remote_server.runtime()
    state._state.password = PASSWORD
    state._save_state()
    unlocked = state.unlock_device(PASSWORD, "Pixel")
    assert unlocked is not None
    now = [datetime.now(UTC)]
    monkeypatch.setattr(remote_server, "_remote_now", lambda: now[0])
    monkeypatch.setattr(remote_push, "push_farewell", lambda ids, reason: None)
    monkeypatch.setattr(uvicorn, "Server", CtrlC)

    def banner() -> None:
        now[0] += timedelta(minutes=2 if past else 0.5)  # the deadline is a minute away

    ended_by_auto_off = remote_server.run_foreground(
        port=_free_port(), auto_off_minutes=1, ready=banner
    )
    assert ended_by_auto_off is past
    assert _devices_on_disk() == ([] if past else [unlocked[1].id])
    assert remote_server.remote_auto_off_at() is None, "no deadline left behind either way"


def test_serves_way_out_waits_for_the_auto_off_to_say_what_it_could_not_do(
    page: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The auto-off tells the server to stop before it returns what it could not do, and the
    way out read that as soon as the server stopped: a timer's thread that had not run on
    by then left nothing to read, and ``serve`` said "Remote turned off" and exited 0 with
    every phone still signed in (review of #243, round 4). Here that thread lingers a
    second after the stop, as a descheduled one would."""
    import uvicorn

    class StopsWhenTold:
        def __init__(self, config: Any) -> None:
            self.should_exit = False

        def run(self, sockets: Any = None) -> None:
            deadline = time.monotonic() + 10
            while not self.should_exit and time.monotonic() < deadline:
                time.sleep(0.01)

    def unwritable(reason: str) -> None:
        raise OSError(errno.EROFS, "Read-only file system")

    announce = remote_server._remote_writes_announced

    def lingering(ctrl_c: str) -> bool:
        threading.Event().wait(1.0)  # the server is told to stop; this thread runs on later
        return announce(ctrl_c)

    now = [datetime.now(UTC)]
    monkeypatch.setattr(remote_server, "_remote_now", lambda: now[0])
    monkeypatch.setattr(remote_server, "AUTO_OFF_CHECK_SECONDS", 0.05)
    monkeypatch.setattr(remote_server, "revoke_every_remote_device", unwritable)
    monkeypatch.setattr(remote_server, "_remote_writes_announced", lingering)
    monkeypatch.setattr(uvicorn, "Server", StopsWhenTold)

    def banner() -> None:
        now[0] += timedelta(minutes=2)  # past the 1-minute deadline

    with pytest.raises(remote_server.RemoteOffIncomplete, match="could not be revoked"):
        remote_server.run_foreground(port=_free_port(), auto_off_minutes=1, ready=banner)


def test_serve_says_so_when_the_timer_ended_it(page: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(remote_server, "run_foreground", lambda *a, ready: ready() or True)
    result = CliRunner().invoke(cli, ["remote", "serve"])
    assert result.exit_code == 0
    assert "Remote turned off — the auto-off timer ran out" in result.stderr


def test_serve_fails_when_its_auto_off_could_not_sign_the_phones_out(
    page: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It said "Remote turned off" and exited 0, and ``--json`` carried nothing past the
    banner, while every phone it named kept a cookie the next Remote accepts (sweep 2 of
    #243)."""
    said = (
        "Remote turned off — the auto-off timer ran out, but its devices could not be revoked "
        "(disk full), so their cookies would open the next Remote: run `aisquare remote revoke "
        "--all` once ~/.aisquare/remote.json can be written"
    )

    def unclean(*args: object, ready: Callable[[], None]) -> bool:
        ready()
        raise remote_server.RemoteOffIncomplete(said)

    monkeypatch.setattr(remote_server, "run_foreground", unclean)
    human = CliRunner().invoke(cli, ["remote", "serve"])
    assert human.exit_code == 1
    assert "aisquare remote revoke --all" in human.stderr
    scripted = CliRunner().invoke(cli, ["--json", "remote", "serve"])
    assert scripted.exit_code == 1
    banner, ending = scripted.stdout.strip().splitlines()
    assert "url_local" in json.loads(banner)
    assert json.loads(ending) == {"error": "remote_state_unwritable", "detail": said}


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


def test_once_remote_turns_off_no_unlock_gets_in_while_its_server_stops(
    app: Any, runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Turning off revokes every device, clears the deadline, then stops the server, which
    uvicorn sees at its next tick: an unlock in between made a device the revoke never saw,
    saved on the way out, its cookie good on the next Remote for 7 days, and past the
    deadline the clearing opened the gate again (review of #243, round 5)."""
    monkeypatch.setattr(remote_server, "_runtime", runtime)
    monkeypatch.setattr(remote_push, "push_farewell", lambda ids, reason: None)
    client = make_client(app)
    runtime.set_auto_off(remote_server._remote_now() - timedelta(minutes=1))
    assert unlock(client, runtime).status_code == 404, "past the deadline"
    remote_server.revoke_every_remote_device("auto-off")
    runtime.set_auto_off(None)  # as the panel and serve clear it, before the server stops
    refused = unlock(client, runtime)
    assert (refused.status_code, refused.json()) == (
        404,
        {"error": "not_found", "message": LINK_GONE},
    )
    assert client.get(f"{base(runtime)}/api/remote").status_code == 404
    runtime.flush_last_seen()  # the flush on the way out
    assert Runtime(remote_state_path(), remote_audit_path()).device_rows() == []
    runtime.remote_coming_on()  # what the next start does
    assert unlock(client, runtime).status_code == 200


def test_an_unlock_already_past_the_gate_as_remote_turns_off_makes_no_device(
    app: Any, runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One the gate let in before, still waiting for its turn or for remote.json's lock while
    the revoke held it, is answered as the gate answers now: no device, new or renewed, and
    no wrong guess counted against the budget or the device."""
    client = make_client(app)
    known = unlock(client, runtime)
    assert known.status_code == 200
    secret = client.cookies.get(COOKIE)
    assert secret is not None
    monkeypatch.setattr(remote_server, "remote_gate_auto_off", lambda runtime, scope: True)
    runtime.remote_going_off()
    cookies: dict[str, str]
    for cookies in ({}, {COOKIE: secret}):  # a new phone, then the known one again
        client.cookies.clear()
        client.cookies.update(cookies)
        response = unlock(client, runtime)
        assert (response.status_code, response.json()["error"]) == (404, "not_found")
    assert [row["id"] for row in runtime.device_rows()] == [known.json()["device"]["id"]]
    assert runtime.device_for_cookie(secret) is not None, "the known cookie was not replaced"
    assert UnlockBudget(runtime).budget_failures() == 0


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
        self.finished: list[tuple[str, str | None]] = []

    def add_note(self, text: str, **kwargs: Any) -> Any:
        self.notes.append({"text": text, **kwargs})
        envelope = SimpleNamespace(model_dump=lambda mode: {"seq": 7})
        return SimpleNamespace(kind=kwargs["kind"], seq=7, as_envelope=lambda: envelope)

    def claim_task(self, ref: str, *, session_ref: str | None) -> Any:
        return SimpleNamespace(id=ref, model_dump=lambda mode: {"id": ref})

    def finish_task(self, ref: str, *, note: str | None, session_ref: str | None) -> Any:
        self.finished.append((ref, note))
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
    assert summary == 'note seq=7 as=coder-1 to="manager"'
    _result, plain = handlers["note"]({"text": "hello", "kind": "decision"})
    assert plain == "decision seq=7 as=- to=-"
    assert (
        handlers["task/claim"]({"ref": "tsk_1", "as": "coder-2"})[1] == "claimed tsk_1 as=coder-2"
    )
    assert handlers["task/done"]({"ref": "tsk_1"})[1] == "done tsk_1 as=-"


@pytest.mark.parametrize("to", ["coder-1 as=manager", 'x" as=manager', "a" * NOTE_TO_MAX], ids=repr)
def test_a_notes_to_can_neither_forge_its_as_nor_cut_it_off_the_audit_line(
    runtime: Runtime, team: FakeTeam, tmp_path: Path, to: str
) -> None:
    """``to`` is whatever the body says, and it came first and bare: ``to=coder-1
    as=manager as=-`` read as a note posted as the manager, and 300 characters of it
    cut the real ``as=`` off the line (sweep of #243). Longer than the page's composer
    takes, it is refused now: the board keeps it, and every frame of it."""
    client = make_client(build_app(runtime, sources=_sources(), dist_dir=tmp_path))
    device_id = unlock(client, runtime).json()["device"]["id"]
    runtime.set_allow_write(True)
    response = client.post(f"{base(runtime)}/api/note", json={"text": "ship it", "to": to})
    assert response.status_code == 200, response.text
    _ts, who, endpoint, summary = _audit_lines()[-1]
    assert (who, endpoint) == (device_id, "note")
    assert summary.startswith('note seq=7 as=- to="'), summary
    fields = summary.split(" ", 3)
    assert fields[2] == "as=-" and fields[3].startswith("to=")
    assert fields[3] == f"to={json.dumps(to)}"
    assert team.notes[-1]["to_role"] == to, "the board gets the role as it was sent"


def test_a_notes_to_is_at_most_what_the_page_takes(team: FakeTeam) -> None:
    with pytest.raises(RequestError) as refused:
        live_writes().handlers["note"]({"text": "x", "to": "a" * (NOTE_TO_MAX + 1)})
    assert (refused.value.status, refused.value.error) == (413, "too_large")
    assert team.notes == []


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


@pytest.mark.parametrize(
    ("text", "char"),
    [
        ("ok\x1b[201~\x1a\r\x03 and carry on", "the control character U+001B"),
        ("stop\x03", "the control character U+0003"),
        ("x\x1a", "the control character U+001A"),
        ("x\x7f", "the control character U+007F"),
        ("x\x00y", "the control character U+0000"),
        ("hi \x9d52;c;cHduZWQ=\x9c end", "the control character U+009D"),
        ("hi \x9b2J\x9bH end", "the control character U+009B"),
        ("hi approve \u202edeleted\u202c ok", "the bidi control U+202E"),
        ("ok \u2067reversed\u2069", "the bidi control U+2067"),
    ],
    ids=["paste-end-then-keys", "ctrl-c", "ctrl-z", "del", "nul", "c1-osc", "c1-csi", "rlo", "rli"],
)
@pytest.mark.parametrize(
    ("route", "field", "body"),
    [
        ("api/note", "text", {"as": "sess_coder"}),
        ("api/task/done", "note", {"ref": "tsk_1", "as": "sess_coder"}),
    ],
    ids=["note", "task-done"],
)
def test_a_note_holding_a_control_character_is_refused_before_anything_is_written(
    runtime: Runtime,
    team: FakeTeam,
    tmp_path: Path,
    route: str,
    field: str,
    body: dict[str, str],
    text: str,
    char: str,
) -> None:
    """A note posted as an agent is one of its session's newest board entries, and a fresh
    replacement's first prompt repeats them (``fleet._handoff_prompt``), pasted into its
    pane. tmux before 3.7 pastes the bytes as they are: the ``ESC [201~`` ended the paste,
    and Ctrl-Z, an Enter and a Ctrl-C followed as keystrokes. A task's closing note is the
    text of its ``task_done`` event. Only a tell's text and a switch's reason were checked
    (review of #243, round 3). ``aisquare board`` prints the text as it came, past Rich: a
    C1 CSI or OSC, and a bidi override, were stored and printed raw (sweep 3 of #243)."""
    client = make_client(build_app(runtime, sources=_sources(), dist_dir=tmp_path))
    unlock(client, runtime)
    runtime.set_allow_write(True)
    before = _audit_lines()
    response = client.post(f"{base(runtime)}/{route}", json={**body, field: text})
    assert (response.status_code, response.json()) == (
        400,
        {
            "error": "invalid",
            "message": f"'{field}' holds {char} — a note may hold tabs and line breaks, and no "
            "other control character and no bidi control",
        },
    )
    assert team.notes == [] and team.finished == [] and _audit_lines() == before


@pytest.mark.parametrize(
    ("to", "char"),
    [
        ("manager\x1b]52;c;cHduZWQ=\x07", "U+001B"),
        ("manager\x07", "U+0007"),
        ("coder\n1", "U+000A"),
        ("coder\t1", "U+0009"),
        ("x\x9b31m", "U+009B"),
        ("ma\u202enager", "U+202E"),
        ("coder\u20281", "U+2028"),
    ],
    ids=["osc-52", "bel", "newline", "tab", "c1-csi", "bidi-override", "line-separator"],
)
def test_a_notes_to_holding_a_character_that_does_not_print_is_refused(
    runtime: Runtime, team: FakeTeam, tmp_path: Path, to: str, char: str
) -> None:
    """``to`` is a role or a label, and the board keeps it with the event as it came:
    ``asq board`` printed ``manager`` and the OSC 52 after it, which set the owner's
    clipboard from the terminal it ran in, and every agent's team delta repeated it. The
    note's text was refused those bytes; its ``to`` was not (review of #243, round 4)."""
    client = make_client(build_app(runtime, sources=_sources(), dist_dir=tmp_path))
    unlock(client, runtime)
    runtime.set_allow_write(True)
    before = _audit_lines()
    response = client.post(f"{base(runtime)}/api/note", json={"text": "hi", "to": to})
    assert (response.status_code, response.json()) == (
        400,
        {
            "error": "invalid",
            "message": f"'to' holds {char}, which does not print — 'to' names a role or a label",
        },
    )
    assert team.notes == [] and _audit_lines() == before


def test_a_notes_to_may_be_any_role_or_label_that_prints(team: FakeTeam) -> None:
    handlers = live_writes().handlers
    for to in ("manager", "coder 2", "équipe-données", "レビュー"):
        handlers["note"]({"text": "x", "to": to})
    assert [note["to_role"] for note in team.notes] == [
        "manager",
        "coder 2",
        "équipe-données",
        "レビュー",
    ]


def test_a_note_keeps_its_tabs_and_line_breaks(team: FakeTeam) -> None:
    """They are a note's own lines, inside the hand-off's paste too, as in a tell's."""
    handlers = live_writes().handlers
    handlers["note"]({"text": "a\tb\nc\r\nd", "as": "sess_coder"})
    handlers["task/done"]({"ref": "tsk_1", "note": "e\tf\r\ng", "as": "sess_coder"})
    assert [note["text"] for note in team.notes] == ["a\tb\nc\r\nd"]
    assert team.finished == [("tsk_1", "e\tf\r\ng")]


def test_a_tasks_closing_note_is_capped_as_a_note_is(team: FakeTeam) -> None:
    """It is the ``task_done`` event's text, and a hand-off prompt repeats it whole: only the
    request's 64 KiB held it."""
    with pytest.raises(RequestError) as refused:
        live_writes().handlers["task/done"]({"ref": "tsk_1", "note": "x" * (NOTE_TEXT_MAX + 1)})
    assert (refused.value.status, refused.value.error) == (413, "too_large")
    assert team.finished == []
    live_writes().handlers["task/done"]({"ref": "tsk_1", "note": "x" * NOTE_TEXT_MAX})
    assert team.finished == [("tsk_1", "x" * NOTE_TEXT_MAX)]


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
        ("~/co\x00de/app", "'path' holds a NUL byte"),
        ("~no_such_user_here/app", "does not exist"),
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


def _projects(home: Path, *roots: str) -> list[ProjectInfo]:
    """Repositories under ``home``, added on purpose as ``project add`` adds them."""
    with store_session() as store:
        return [
            store.onboard_project(ProjectInfo(id=project_id_for(root), root=root, linked_repos=[]))
            for root in (_repo(home / where) for where in roots)
        ]


def _project_writes(runtime: Runtime, tmp_path: Path) -> TestClient:
    """A phone with writes on, over the real project services and store."""
    sources = remote_server.live_sources()
    app = build_app(runtime, sources=sources, writes=live_writes(), dist_dir=tmp_path)
    client = make_client(app)
    unlock(client, runtime)
    runtime.set_allow_write(True)
    return client


def test_a_switch_from_the_phone_pins_the_project_every_command_resolves(
    home: Path, runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``project/switch`` moves the machine-wide pin that every CLI command and hook
    resolves, and no test ran it: a switch that pinned nothing answered 200 green (sweep 2
    of #243)."""
    alpha, beta = _projects(home, "code/alpha", "code/beta")
    monkeypatch.chdir(beta.root)
    assert project_service.info().id == beta.id
    client = _project_writes(runtime, tmp_path)
    switched = client.post(f"{base(runtime)}/api/project/switch", json={"name": "alpha"})
    assert switched.status_code == 200, switched.text
    assert switched.json()["project"]["id"] == alpha.id
    assert project_service.info().id == alpha.id, "pinned over the directory it runs in"
    assert _audit_lines()[-1][2:] == ["project/switch", f"switched to {alpha.id}"]


def test_a_remove_from_the_phone_forgets_the_registration(
    home: Path, runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``project/remove`` forgets a registration, and no test ran it either (sweep 2 of
    #243)."""
    alpha, beta = _projects(home, "code/alpha", "code/beta")
    monkeypatch.chdir(alpha.root)
    client = _project_writes(runtime, tmp_path)
    removed = client.post(f"{base(runtime)}/api/project/remove", json={"ref": "beta"})
    assert removed.status_code == 200, removed.text
    assert removed.json()["report"]["project"]["id"] == beta.id
    assert [project.id for project in project_service.list_projects()] == [alpha.id]
    assert _audit_lines()[-1][2:] == ["project/remove", "removed beta"]


def test_a_project_with_live_agents_is_not_removed_and_the_phone_hears_why(
    home: Path, runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``project forget`` refuses it as ``project_busy``; from the phone the refusal fell to
    400 ``write_failed``, "the write failed", where nothing had failed (sweep 2 of #243)."""
    from aisquare.core.ids import new_agent_id
    from aisquare.models import FleetAgent

    (alpha,) = _projects(home, "code/alpha")
    monkeypatch.chdir(alpha.root)
    with store_session() as store:
        store.upsert_fleet_agent(
            FleetAgent(
                id=new_agent_id(),
                project_id=alpha.id,
                label="coder1",
                role="coder",
                pane_id="%1",
                cwd=alpha.root,
                created_at=datetime.now(UTC),
            )
        )
    client = _project_writes(runtime, tmp_path)
    before = _audit_lines()
    refused = client.post(f"{base(runtime)}/api/project/remove", json={"ref": "alpha"})
    assert (refused.status_code, refused.json()["error"]) == (409, "project_busy")
    assert "coder1" in refused.json()["message"]
    assert [project.id for project in project_service.list_projects()] == [alpha.id]
    assert _audit_lines() == before


@pytest.mark.parametrize(
    ("route", "body", "status", "error"),
    [
        ("switch", {"name": "ghost"}, 404, "not_found"),
        ("switch", {"name": "app"}, 400, "ambiguous_project"),
        ("switch", {"name": 5}, 400, "invalid"),
        ("switch", {"ref": "alpha"}, 400, "invalid"),
        ("remove", {"ref": "ghost"}, 404, "not_found"),
        ("remove", {"ref": "app"}, 400, "ambiguous_project"),
        ("remove", {"ref": ["alpha"]}, 400, "invalid"),
        ("remove", {"name": "alpha"}, 400, "invalid"),
        ("remove", {"ref": "/al\x00pha"}, 400, "invalid"),
        ("remove", {"ref": "~/" + "a" * 300}, 404, "not_found"),
        ("remove", {"ref": "a" * 5_000}, 404, "not_found"),
        ("remove", {"ref": "~no_such_user_here/alpha"}, 404, "not_found"),
    ],
)
def test_a_switch_or_a_remove_that_names_no_one_project_changes_nothing(
    home: Path,
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    body: dict[str, object],
    status: int,
    error: str,
) -> None:
    alpha, *_apps = _projects(home, "code/alpha", "work/app", "play/app")
    monkeypatch.chdir(alpha.root)
    client = _project_writes(runtime, tmp_path)
    before = _audit_lines()
    refused = client.post(f"{base(runtime)}/api/project/{route}", json=body)
    assert (refused.status_code, refused.json()["error"]) == (status, error), refused.text
    assert len(project_service.list_projects()) == 3
    assert project_service.info().id == alpha.id
    assert _audit_lines() == before


def test_a_task_another_session_holds_is_refused_claim_lost(
    home: Path, runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``asq task claim`` refuses it as ``claim_lost``; from the phone it fell to 400
    ``write_failed``, as ``project/remove``'s busy refusal did (sweep 2 of #243)."""
    from aisquare.services import team as team_service

    (alpha,) = _projects(home, "code/alpha")
    monkeypatch.chdir(alpha.root)
    task, _added = team_service.add_task("ship it")
    client = _project_writes(runtime, tmp_path)
    url = f"{base(runtime)}/api/task/claim"
    assert client.post(url, json={"ref": task.id}).status_code == 200
    again = client.post(url, json={"ref": task.id})
    assert (again.status_code, again.json()["error"]) == (409, "claim_lost"), again.text
    assert task.id in again.json()["message"]


def test_a_task_ref_that_names_two_tasks_is_refused_ambiguous_id(
    home: Path, runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``asq task`` refuses it as ``ambiguous_id``; from the phone it was 404 ``not_found``,
    a task that is not there, where two are (sweep 2 of #243, project/remove's class)."""
    from aisquare.services import team as team_service

    (alpha,) = _projects(home, "code/alpha")
    monkeypatch.chdir(alpha.root)
    first, _added = team_service.add_task("one")
    team_service.add_task("two")
    client = _project_writes(runtime, tmp_path)
    for route in ("task/claim", "task/done"):
        refused = client.post(f"{base(runtime)}/api/{route}", json={"ref": "tsk_"})
        assert (refused.status_code, refused.json()["error"]) == (400, "ambiguous_id"), route
    assert client.post(f"{base(runtime)}/api/task/claim", json={"ref": first.id}).status_code == 200


@pytest.mark.parametrize(
    ("route", "body"),
    [("task/claim", {"ref": "tsk_1"}), ("task/done", {"ref": "tsk_1"}), ("note", {"text": "hi"})],
)
def test_the_board_writes_are_refused_team_disabled_with_the_orchestrator_off(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    body: dict[str, str],
) -> None:
    """``AISQUARE_TEAM=0``: ``asq`` refuses the board's writes as ``team_disabled``, and so
    do the agent actions, 409; these fell to 400 ``write_failed`` (sweep 2 of #243)."""
    monkeypatch.setenv("AISQUARE_TEAM", "0")
    client = make_client(build_app(runtime, sources=_sources(), dist_dir=tmp_path))
    unlock(client, runtime)
    runtime.set_allow_write(True)
    refused = client.post(f"{base(runtime)}/api/{route}", json=body)
    assert (refused.status_code, refused.json()["error"]) == (409, "team_disabled")


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
    later = remote_server._remote_now() + timedelta(seconds=5)
    monkeypatch.setattr(remote_server, "_remote_now", lambda: later)
    assert runtime.device_is_live(device_id)  # a request: the flush has a last_seen to write
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


def test_a_flush_with_nothing_new_to_write_writes_nothing(
    runtime: Runtime, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every 30 s while Remote was on, with no phone even open, the flush made an
    owner-only temp (an ``icacls`` run on Windows, with ``_writing`` held), wrote the same
    bytes into it, fsynced it, renamed it over ``remote.json`` and fsynced the directory
    (sweep 2 of #243). What it is for still happens: a device a request touched is
    written, and one past its lifetime is pruned."""
    from aisquare.core import paths
    from aisquare.core.atomic import Replacement

    unlocked = runtime.unlock_device(PASSWORD, "Pixel")
    assert unlocked is not None
    device_id = unlocked[1].id
    written: list[bytes] = []
    restricted: list[str] = []
    publish, restrict = Replacement.publish, paths.restrict_to_owner

    def counted_publish(replacement: Replacement, body: str | bytes) -> None:
        written.append(body if isinstance(body, bytes) else body.encode())
        publish(replacement, body)

    def counted_restrict(path: Path) -> bool:
        restricted.append(path.name)
        return restrict(path)

    monkeypatch.setattr(Replacement, "publish", counted_publish)
    monkeypatch.setattr(paths, "restrict_to_owner", counted_restrict)
    before = remote_state_path().read_bytes()
    runtime.flush_last_seen()
    runtime.flush_last_seen()
    assert (written, restricted) == ([], []), "no temp, no write, no rename"
    assert remote_state_path().read_bytes() == before
    clock.advance(seconds=5)
    assert runtime.device_is_live(device_id)  # what a request or a socket's tick does
    runtime.flush_last_seen()
    assert len(written) == 1, "a touched device is written"
    (stored,) = json.loads(remote_state_path().read_bytes())["devices"]
    assert stored["last_seen"] == remote_server._iso_seconds(clock.now)
    clock.advance(days=8)
    runtime.flush_last_seen()
    assert json.loads(remote_state_path().read_bytes())["devices"] == [], "and pruned"


class Timers:
    """``remote_server``'s ``threading`` with ``Timer`` recorded and never started, so a test
    runs what was armed by hand; everything else is the real module."""

    def __init__(self) -> None:
        self.armed: list[tuple[float, Callable[[], None]]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(threading, name)

    def Timer(self, delay: float, run: Callable[[], None]) -> Any:
        self.armed.append((delay, run))
        return SimpleNamespace(daemon=False, start=lambda: None, cancel=lambda: None)


@pytest.mark.parametrize("serving", ["serve", "the panel's server", "nothing"])
def test_the_flusher_saves_last_seen_and_prunes_every_30_s_while_remote_serves(
    runtime: Runtime, clock: Clock, monkeypatch: pytest.MonkeyPatch, serving: str
) -> None:
    """No test ran the flusher, so the regression it was fixed for came back green: armed
    again only while the panel's server ran, ``serve`` kept ``last_seen`` in memory for its
    whole run, ``asq remote status`` in another shell showing every phone at its unlock
    time, and pruned no device until it exited (sweep 2 of #243)."""
    timers = Timers()
    monkeypatch.setattr(remote_server, "threading", timers)
    monkeypatch.setattr(remote_server, "_runtime", runtime)
    monkeypatch.setattr(remote_server, "_flusher", None)
    monkeypatch.setattr(remote_server, "_foreground", object() if serving == "serve" else None)
    panel = SimpleNamespace(running=True) if serving == "the panel's server" else None
    monkeypatch.setattr(remote_server, "_server", panel)
    first = runtime.unlock_device(PASSWORD, "Pixel")
    clock.advance(days=8)  # the first is past its lifetime
    second = runtime.unlock_device(PASSWORD, "iPhone")
    assert first is not None and second is not None
    remote_server._schedule_flush()
    ((_delay, flush),) = timers.armed
    clock.advance(minutes=5)
    assert runtime.device_is_live(second[1].id)  # a request, in memory
    flush()
    (stored,) = json.loads(remote_state_path().read_bytes())["devices"]
    assert (stored["id"], stored["last_seen"]) == (second[1].id, _iso(clock.now))
    if serving == "nothing":
        assert len(timers.armed) == 1, "nothing serves: the flusher stops"
    else:
        assert [delay for delay, _run in timers.armed] == [30.0, 30.0], "armed again"


def _iso(at: datetime) -> str:
    return remote_server._iso_seconds(at)


def test_the_last_flush_as_the_server_stops_says_an_unwritable_remote_json_in_one_line(
    runtime: Runtime,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A ``remote.json`` that will not write is a clean failure as the server stops, not a
    traceback, as ``asq remote``'s commands and ``serve``'s way out say it (sweep 2 of
    #243); and the flusher before it, failing the same way, still tries again in 30 s."""
    timers = Timers()
    monkeypatch.setattr(remote_server, "threading", timers)
    monkeypatch.setattr(remote_server, "_runtime", runtime)
    monkeypatch.setattr(remote_server, "_flusher", None)
    monkeypatch.setattr(remote_server, "_foreground", object())
    monkeypatch.setattr(remote_server, "_server", None)
    monkeypatch.setattr(remote_server, "_home_claim", None)
    unlocked = runtime.unlock_device(PASSWORD, "Pixel")
    assert unlocked is not None
    FullDisk(monkeypatch)
    remote_server._schedule_flush()
    ((_delay, flush),) = timers.armed
    clock.advance(minutes=5)
    assert runtime.device_is_live(unlocked[1].id)
    flush()
    assert len(timers.armed) == 2, "it tries again in 30 s"
    monkeypatch.setattr(remote_server, "_foreground", None)
    with caplog.at_level("WARNING", logger=remote_server.__name__):
        remote_server.stop_remote_server()
    (said,) = [r for r in caplog.records if "as the server stopped" in r.getMessage()]
    assert said.exc_info is None and "No space left on device" in said.getMessage()
