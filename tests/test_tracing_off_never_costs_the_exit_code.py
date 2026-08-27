"""An observer that fails your build is not an observer.

The doctrine's exact words are "tracing is an observer — it may cost a trace,
never a launch, an exit code, or a millisecond on the primary path". The
first-party rows honour that: ``_live_checks`` picks ``degrade = _fail if on
else _warn`` once, and every row it emits goes through it, so with tracing off
an unreachable gateway reads ⚠ and the exit code does not move.

``_sdk_checks`` did not take that argument. It mapped the SDK doctor's own
status strings straight to ``_fail``, so the SAME unreachable gateway produced::

    ⚠ explainability gateway: http://127.0.0.1:1/ready — unreachable: [Errno 111]
    ✗ sdk:gateway_live:  [Errno 111] Connection refused
    ✗ sdk:gateway_ready: [Errno 111] Connection refused

Measured live against real stg on 2026-08-18, tracing off, both directions:
gateway UP → ``doctor --live`` exit 0, 0 fails; gateway DOWN → exit 1, 2 fails,
both of them ``sdk:``. One condition, two verdicts, and the louder one wins the
exit code.

WHY THIS IS AN 08:00 DEFECT. The runbook has the operator configure the gateway
BEFORE it is reachable — that is what §2 and §3 are for — so "configured, not
enabled, not yet reachable" is the normal pre-cutover state, and in it
``doctor --live`` exited 1. Anything gating on that exit code reads a
not-yet-configured deployment as a broken one.

THE CALL, MADE DELIBERATELY: all sdk rows degrade, not just gateway-shaped ones.
The section already decides this once, by asking "is tracing on" and never "is
this particular failure about the gateway" — and a rule you can state in one
sentence is worth more here than per-row cleverness, because the reader at 08:00
has to predict the output without reading the source.

WHAT MUST NOT REGRESS, and the reason this file has controls: the rows stay
VISIBLE (warn, with the reason intact), because hiding them is the defect
tsk_01m0ak70 just fixed; and tracing ON keeps failing, because degrading
unconditionally would let a genuinely broken traced deployment exit 0.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from aisquare.core.config import ExplainabilitySettings, ExplainabilityTarget
from aisquare.models import CheckStatus, DoctorCheck
from aisquare.services import explainability_ops as ops
from tests.test_explainability_ops import _LIVE_ROUTES, _NO_PROXY, _env, _gateway

#: What the SDK doctor reports when the gateway it was pointed at is down.
#: Verbatim from a live run, so the mapping is exercised on a real string.
_SDK_DOWN = [
    ("sdk_version", "ok", "1.0.6"),
    ("gateway_live", "error", "[Errno 111] Connection refused"),
    ("gateway_ready", "error", "[Errno 111] Connection refused"),
]


@pytest.fixture
def sdk_down(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(ops, "sdk_doctor", lambda: list(_SDK_DOWN))
    yield


def _settings(gateway_url: str, *, on: bool) -> ExplainabilitySettings:
    """`_wired` hardcodes enabled=True and forwards overrides to the TARGET, so
    the one knob this file turns is the one it cannot reach. Built here."""
    return ExplainabilitySettings(
        enabled=on,
        targets={"stg": ExplainabilityTarget(gateway_url=gateway_url, proxy_url=_NO_PROXY)},
    )


def _rows(*, on: bool) -> list[DoctorCheck]:
    """A healthy gateway, so the ONLY red thing available is the sdk mapping."""
    server, url, _ = _gateway(dict(_LIVE_ROUTES))
    try:
        return ops.checks(_settings(url, on=on), live=True, env=_env())
    finally:
        server.shutdown()
        server.server_close()


def _sdk(rows: list[DoctorCheck]) -> list[DoctorCheck]:
    return [r for r in rows if r.name.startswith("sdk:")]


def test_tracing_off_turns_an_sdk_failure_into_a_warning(sdk_down: None) -> None:
    """The defect, stated as the property rather than as the exit code."""
    broken = [r for r in _sdk(_rows(on=False)) if "Connection refused" in r.detail]

    assert broken, "fixture premise: the sdk doctor must report something red"
    assert all(r.status is CheckStatus.warn for r in broken), [(r.name, r.status) for r in broken]


def test_tracing_off_means_nothing_here_can_fail(sdk_down: None) -> None:
    """The exit code is what actually broke, so it gets its own assertion.

    `emit_doctor` moves the exit code on `fail` and never on `warn`, so "no
    fail rows in the whole section" IS the exit-code claim, expressed without
    shelling out to the CLI.
    """
    assert [r.name for r in _rows(on=False) if r.status is CheckStatus.fail] == []


def test_tracing_on_still_fails(sdk_down: None) -> None:
    """The control that keeps the fix from being a mute button.

    A genuinely broken traced deployment must not exit 0. Without this, the
    one-line fix "always warn" passes every other test in this file.
    """
    failed = [r.name for r in _sdk(_rows(on=True)) if r.status is CheckStatus.fail]

    assert "sdk:gateway_live" in failed, failed
    assert "sdk:gateway_ready" in failed, failed


def test_the_degraded_row_still_says_what_went_wrong(sdk_down: None) -> None:
    """Softening the STATUS must not soften the REASON.

    A warn that dropped the errno would be the tsk_01m0ak70 defect wearing a
    different colour: present, and uninformative.
    """
    row = next(r for r in _sdk(_rows(on=False)) if r.name == "sdk:gateway_live")

    assert "Connection refused" in row.detail


def test_the_rows_are_still_all_there(sdk_down: None) -> None:
    """Degrading is not filtering — the count is identical either way."""
    assert [r.name for r in _sdk(_rows(on=False))] == [r.name for r in _sdk(_rows(on=True))]


def test_a_healthy_sdk_is_unchanged_by_any_of_this(monkeypatch: pytest.MonkeyPatch) -> None:
    """The control for the whole file: green stays green in both states."""
    monkeypatch.setattr(ops, "sdk_doctor", lambda: [("sdk_version", "ok", "1.0.6")])

    for on in (True, False):
        row = next(r for r in _sdk(_rows(on=on)) if r.name == "sdk:sdk_version")
        assert row.status is CheckStatus.ok, (on, row.status)


@pytest.mark.parametrize("status", ["warning", "warn", "missing"])
def test_rows_that_were_already_warnings_do_not_move(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    """Only the fail branch was wrong; the warn branch was always exit-neutral."""
    monkeypatch.setattr(ops, "sdk_doctor", lambda: [("httpx", status, "not installed")])

    for on in (True, False):
        row = next(r for r in _sdk(_rows(on=on)) if r.name == "sdk:httpx")
        assert row.status is CheckStatus.warn, (on, status, row.status)
