"""The captain's owner gate (#216): a TOTP code opens a 15-minute act-as-owner window.

RFC 6238 (30-second steps, one step of drift, a code never accepted twice) and RFC 4226
§7.3 (throttling: three wrong codes in a row lock entry, and the board says so). Every test
here reads ``[captain] gate`` as a user's config would (the ``captain_gate`` marker); the
rest of the suite runs with the gate off, as Phase 1 had none.
"""

from __future__ import annotations

import json
import stat
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

import pyotp
import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import paths
from aisquare.core.store import store_session
from aisquare.services.captain import actions, gate
from aisquare.services.captain import state as captain_state
from aisquare.services.captain.errors import Refused

pytestmark = [pytest.mark.captain_gate, pytest.mark.needs_captain]

T0 = datetime(2026, 10, 23, 20, 0, 0, tzinfo=UTC)


def _config(text: str) -> None:
    paths.config_path().parent.mkdir(parents=True, exist_ok=True)
    paths.config_path().write_text(text, encoding="utf-8")


def _code(at: datetime) -> str:
    """The code an authenticator shows at ``at`` for the enrolled secret."""
    secret = gate.secret_path().read_text(encoding="ascii").strip()
    return str(pyotp.TOTP(secret, interval=gate.STEP_SECONDS).at(at))


def _audit() -> list[dict[str, Any]]:
    with store_session() as store:
        events = store.filtered_events(
            captain_state.home_project().id, kind="captain_action", since_seq=0, limit=500
        )
    return [json.loads(event.text) for event in events]


def _press_like() -> str:
    """A privileged call through the frame, with a body that only says it ran."""
    return actions._run("press", {"label": "coder-1"}, "press enter", lambda _: _ran())


def _ran() -> actions.Outcome:
    return actions.Outcome({}, said="pressed")


def test_with_the_gate_switched_off_nothing_changes() -> None:
    """`[captain] gate = false`: Phase 1, every tool as it was."""
    _config("[captain]\ngate = false\n")
    assert gate.admit("press") is None
    assert json.loads(_press_like())["action_seq"] > 0


def test_the_gate_applies_only_while_the_captain_is_on(monkeypatch: pytest.MonkeyPatch) -> None:
    from aisquare.core import experimental

    monkeypatch.delenv(experimental.CAPTAIN_ENV, raising=False)
    assert gate.active() is False
    assert gate.admit("press") is None


def test_unenrolled_the_gate_fails_closed_and_says_how() -> None:
    """On by default: until a secret is enrolled, the tools that act as the owner refuse."""
    with pytest.raises(Refused, match="aisquare captain gate enroll"):
        _press_like()
    (event,) = _audit()
    assert event["tool"] == "press" and event["ok"] is False
    assert "acts as the owner and the gate is locked" in event["said"]


def test_without_pyotp_the_gate_names_its_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    gate.enroll()
    monkeypatch.setitem(sys.modules, "pyotp", None)  # an import now raises ImportError
    with pytest.raises(Refused, match=r"pip install 'aisquare-cli\[gate\]'"):
        gate.admit("press")


def test_reads_tell_and_ask_manager_never_need_the_gate() -> None:
    gate.enroll()
    for tool in ("board", "attention", "since", "read_pane", "tell", "ask_manager", "note"):
        assert gate.admit(tool) is None, tool


def test_a_code_opens_a_15_minute_window_and_then_the_gate_locks_again() -> None:
    gate.enroll()
    with pytest.raises(Refused, match="gate is locked"):
        gate.admit("press", now=T0)
    window = gate.unlock(_code(T0), now=T0)
    assert window.until == T0 + gate.WINDOW
    assert gate.admit("press", now=T0 + timedelta(minutes=14)) == window.id
    with pytest.raises(Refused, match="gate is locked"):
        gate.admit("press", now=T0 + timedelta(minutes=16))


def test_the_window_id_joins_the_privileged_calls_audit() -> None:
    gate.enroll()
    window = gate.unlock(_code(datetime.now(UTC)))
    _press_like()
    press = [event for event in _audit() if event["tool"] == "press"]
    assert press and press[-1]["ok"] is True and press[-1]["window"] == window.id


def test_a_code_is_never_accepted_twice_and_drift_is_one_step() -> None:
    gate.enroll()
    gate.unlock(_code(T0), now=T0)
    with pytest.raises(Refused, match="already used"):
        gate.unlock(_code(T0), now=T0 + timedelta(seconds=5))
    later = T0 + timedelta(seconds=gate.STEP_SECONDS)
    assert gate.unlock(_code(later), now=later).until == later + gate.WINDOW, "the next step's"
    # One step of drift either side is accepted; two is not.
    fresh = T0 + timedelta(minutes=10)
    gate.unlock(_code(fresh - timedelta(seconds=gate.STEP_SECONDS)), now=fresh)
    stale = fresh + timedelta(minutes=1)
    with pytest.raises(Refused, match="wrong code"):
        gate.unlock(_code(stale - timedelta(seconds=2 * gate.STEP_SECONDS)), now=stale)


def test_three_wrong_codes_in_a_row_lock_entry_and_say_so_on_the_board() -> None:
    """RFC 4226 §7.3's throttle is the issue's rule: the third wrong code in a row locks
    entry for five minutes, and its audited refusal on the home board says why."""
    gate.enroll()
    for _ in range(2):
        with pytest.raises(Refused, match="wrong code"):
            actions.perform("unlock", {"code": "000000"}, "captain, unlock 000000")
    with pytest.raises(Refused, match="3 times in a row"):
        actions.perform("unlock", {"code": "000000"}, "captain, unlock 000000")
    said = [event["said"] for event in _audit() if event["tool"] == "unlock"]
    assert len(said) == 3 and "3 times in a row" in said[-1]
    now = datetime.now(UTC)
    with pytest.raises(Refused, match="locked until"):
        gate.unlock(_code(now), now=now)
    after = now + gate.LOCKOUT + timedelta(minutes=1)
    assert gate.unlock(_code(after), now=after).until == after + gate.WINDOW


def test_the_audit_never_keeps_the_code() -> None:
    """The code is said aloud or typed, and the words go into the audit: masked there."""
    gate.enroll()
    code = _code(datetime.now(UTC))
    actions.perform("unlock", {"code": code}, f"captain, unlock {code[:3]} {code[3:]}")
    (event,) = [event for event in _audit() if event["tool"] == "unlock"]
    text = json.dumps(event)
    assert code not in text and f"{code[:3]} {code[3:]}" not in text
    assert event["args"]["code"] == "******" and "******" in event["utterance"]


def test_lock_closes_the_window_at_once() -> None:
    gate.enroll()
    gate.unlock(_code(datetime.now(UTC)))
    assert json.loads(actions.lock("lock it"))["was_open"] is True
    with pytest.raises(Refused, match="gate is locked"):
        gate.admit("press")


def test_enroll_writes_a_private_secret_and_refuses_to_overwrite_it(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--json", "captain", "gate", "enroll"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["uri"].startswith("otpauth://totp/")
    assert "issuer=aisquare%20captain" in payload["uri"]
    mode = stat.S_IMODE(gate.secret_path().stat().st_mode)
    assert mode == 0o600 or sys.platform == "win32"
    again = runner.invoke(app, ["captain", "gate", "enroll"])
    assert again.exit_code != 0 and "--replace" in again.output
    assert runner.invoke(app, ["captain", "gate", "enroll", "--replace"]).exit_code == 0


def test_from_the_terminal_a_locked_gate_refuses_a_verb_and_a_code_opens_it(
    runner: CliRunner,
) -> None:
    """The owner's own CLI verbs go through the same frame, gated alike."""
    runner.invoke(app, ["captain", "gate", "enroll"])
    refused = runner.invoke(app, ["captain", "wololo", "nowhere", "coder-1", "t1"])
    assert refused.exit_code != 0 and "acts as the owner and the gate is locked" in refused.output
    code = _code(datetime.now(UTC))
    unlocked = runner.invoke(app, ["captain", "gate", "unlock", code])
    assert unlocked.exit_code == 0, unlocked.output
    assert "unlocked until" in unlocked.output and "window w-" in unlocked.output
    status = json.loads(runner.invoke(app, ["--json", "captain", "gate", "status"]).stdout)
    assert status["active"] is True and status["window"] and "secret" not in status
    after = runner.invoke(app, ["captain", "wololo", "nowhere", "coder-1", "t1"])
    assert "gate is locked" not in after.output, "the window is open: the gate lets it by"


def _wrong(at: datetime) -> str:
    """A six-digit code no step within the drift accepts at ``at``."""
    valid = {_code(at + timedelta(seconds=gate.STEP_SECONDS * k)) for k in (-1, 0, 1)}
    return next(code for code in ("000000", "111111", "222222", "333333") if code not in valid)


def _lock_once(at: datetime) -> str:
    """Three wrong codes in a row at ``at``: the third's refusal."""
    for _ in range(gate.THROTTLE - 1):
        with pytest.raises(Refused, match="wrong code"):
            gate.unlock(_wrong(at), now=at)
    with pytest.raises(Refused) as third:
        gate.unlock(_wrong(at), now=at)
    return str(third.value)


def test_each_lock_in_a_row_lasts_twice_the_last_up_to_a_day_until_a_correct_code() -> None:
    """RFC 4226 §7.3: the delay increases. A flat five minutes let a patient guesser try
    ~864 codes a day (review of #339); each lock with no correct code between doubles."""
    gate.enroll()
    at, said = T0, []
    for minutes in (5, 10, 20, 40):
        said.append(_lock_once(at))
        almost = at + timedelta(minutes=minutes) - timedelta(seconds=1)
        with pytest.raises(Refused, match="locked until"):
            gate.unlock(_code(almost), now=almost)
        at += timedelta(minutes=minutes, seconds=1)
    assert [refusal.rsplit("locked for ", 1)[1] for refusal in said] == [
        "5 minutes",
        "10 minutes",
        "20 minutes",
        "40 minutes",
    ]
    assert gate.lockout(9) == timedelta(hours=21, minutes=20)
    assert gate.lockout(10) == gate.lockout(400) == gate.LOCKOUT_MAX == timedelta(hours=24)
    assert gate.unlock(_code(at), now=at).until == at + gate.WINDOW
    later = at + timedelta(minutes=1)
    assert _lock_once(later).endswith("locked for 5 minutes"), "a correct code resets it"


def test_a_code_said_alongside_another_command_is_masked_in_that_tools_audit() -> None:
    """ "Unlock 123 456 and press enter": the press's audit keeps the owner's words, and
    the code in them is masked there too, not only in unlock's (review of #339)."""
    gate.enroll()
    code = _code(datetime.now(UTC))
    gate.unlock(code)
    spoken = f"captain, unlock {code[:3]} {code[3:]} and press enter on coder-1"
    actions._run("press", {"label": "coder-1"}, spoken, lambda _: _ran())
    (event,) = [event for event in _audit() if event["tool"] == "press"]
    assert f"{code[:3]} {code[3:]}" not in json.dumps(event)
    assert event["utterance"] == "captain, unlock ****** and press enter on coder-1"


@pytest.mark.parametrize(
    ("words", "kept"),
    [
        ("unlock 123456", "unlock ******"),
        ("123 456", "******"),
        ("1 2 3 4 5 6", "******"),
        ("123-456", "******"),
        ("on 2026-10-23 at 12:30", "on 2026-10-23 at 12:30"),
        ("ticket 1234567", "ticket 1234567"),
        ("stop coder-1 and look at PR 339", "stop coder-1 and look at PR 339"),
        # A code joined to a word or trailed by a dash is still a code (review of #339).
        ("code-123456", "code-******"),
        ("unlock 123456- and press", "unlock ******- and press"),
        ("unlock 123-456-", "unlock ******-"),
        ("call +1-555-123-4567", "call +1-555-123-4567"),
        ("build 2026-10-23-1", "build 2026-10-23-1"),
        # An identifier whose random part has six digits in a row is no code (rc/p3 run
        # 40073709259: a task id in wololo's audit came out masked).
        ("wololo alpha coder-1 tsk_01m5pcq423948pmfre2mvs761d", None),
        ("claim tsk_01m5pcq423948", None),
        ("session 123456ab-cdef", None),
        ("at a1b2c3d123456 now", None),
    ],
)
def test_redact_masks_a_code_but_not_a_date_or_a_longer_number(
    words: str, kept: str | None
) -> None:
    assert gate.redact(words) == (words if kept is None else kept)
