"""The device-grant wait as the Accounts card runs it: RFC 8628 §3.4 with a cancel flag.

``services.device_flow.wait_for_token`` is the terminal's poll loop with the
clock, the sleep and the cancel check as parameters, so every branch is driven
here with a scripted ``iam.poll_token`` and a fake clock, and never a real
sleep. Each positive claim has its negative: the token that IS returned beside
the one that is refused because Cancel landed while the poll was in flight.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from aisquare.services import device_flow, iam

ENDPOINTS = iam.Endpoints(
    issuer="https://api.example/o",
    device_authorization="https://api.example/o/device-authorization/",
    token="https://api.example/o/token/",
    userinfo="https://api.example/o/userinfo/",
    revocation="https://api.example/o/revoke_token/",
)
GRANT = iam.DeviceAuthorization(
    device_code="dev",
    user_code="WDJB-MJHT",
    verification_uri="https://home.example/cli",
    verification_uri_complete="https://home.example/cli?code=WDJB-MJHT",
    expires_in=900,
    interval=5,
)
TOKEN = {"access_token": "aisq_new", "expires_in": 7776000}


class Clock:
    """A monotonic clock that only moves when the loop sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []
        self.poll_times: list[float] = []

    def waits(self) -> list[float]:
        """How long the loop waited before each poll, from the clock at each poll."""
        starts = [1000.0, *self.poll_times[:-1]]
        return [round(at - start, 3) for start, at in zip(starts, self.poll_times, strict=True)]

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


def _wait(
    outcomes: list[iam.PollOutcome | Exception],
    *,
    cancelled: Callable[[], bool] = lambda: False,
    clock: Clock | None = None,
) -> tuple[dict[str, Any], Clock, list[int]]:
    """Run the wait against scripted poll answers; returns the token, the clock, the polls made."""
    clock = clock or Clock()
    polls: list[int] = []

    def poll(endpoints: iam.Endpoints, device_code: str) -> iam.PollOutcome:
        assert endpoints is ENDPOINTS and device_code == "dev"
        polls.append(len(polls))
        clock.poll_times.append(clock.now)
        answer = outcomes[min(len(polls) - 1, len(outcomes) - 1)]
        if isinstance(answer, Exception):
            raise answer
        return answer

    token = device_flow.wait_for_token(
        ENDPOINTS,
        GRANT,
        cancelled=cancelled,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        jitter=lambda a, b: 0.0,
        poll=poll,
    )
    return token, clock, polls


def test_pending_then_token_returns_the_token_after_the_interval() -> None:
    token, clock, polls = _wait([iam.PollOutcome("pending"), iam.PollOutcome("token", TOKEN)])
    assert token == TOKEN
    assert polls == [0, 1]
    # interval + 20 % (jitter scripted to 0), twice, in half-second steps.
    assert sum(clock.slept) == pytest.approx(12.0)
    assert max(clock.slept) <= device_flow.CANCEL_POLL_SECONDS


def test_slow_down_widens_the_interval_and_rate_limited_waits_retry_after_capped() -> None:
    _, clock, _ = _wait(
        [
            iam.PollOutcome("slow_down", interval=10),
            iam.PollOutcome("rate_limited", retry_after=90),
            iam.PollOutcome("token", TOKEN),
        ]
    )
    assert clock.waits() == [6.0, 12.0, float(device_flow.MAX_RETRY_AFTER_SECONDS)]


def test_an_unreachable_endpoint_backs_off_and_caps() -> None:
    unreachable = iam.IamError("unreachable", "no route")
    _, clock, polls = _wait(
        [unreachable, unreachable, unreachable, iam.PollOutcome("token", TOKEN)]
    )
    assert polls == [0, 1, 2, 3]
    assert clock.waits() == [6.0, 10.0, 20.0, 40.0]  # doubling from the interval, never below it
    with pytest.raises(iam.IamError, match="boom"):
        _wait([iam.IamError("invalid_grant", "boom")])  # any other code propagates at once


@pytest.mark.parametrize(
    ("kind", "code"),
    [("denied", "access_denied"), ("expired", "expired"), ("paused", "paused")],
)
def test_terminal_answers_raise_the_terminals_codes(kind: str, code: str) -> None:
    with pytest.raises(iam.IamError) as caught:
        _wait([iam.PollOutcome(kind)])
    assert caught.value.code == code


def test_an_unknown_answer_is_an_unsupported_server() -> None:
    with pytest.raises(iam.IamError) as caught:
        _wait([iam.PollOutcome("frobnicate")])
    assert caught.value.code == "unsupported_server"


def test_the_deadline_ends_the_wait_as_expired() -> None:
    clock = Clock()
    with pytest.raises(iam.IamError) as caught:
        _wait([iam.PollOutcome("pending")], clock=clock)
    assert caught.value.code == "expired"
    assert clock.now - 1000.0 >= GRANT.expires_in + device_flow.GRACE_SECONDS


def test_cancel_during_a_wait_stops_within_half_a_second_and_polls_nothing() -> None:
    flag = {"set": False}
    clock = Clock()
    original_sleep = clock.sleep

    def sleep(seconds: float) -> None:
        original_sleep(seconds)
        if clock.now - 1000.0 >= 1.0:
            flag["set"] = True

    clock.sleep = sleep  # type: ignore[method-assign]
    with pytest.raises(iam.IamError) as caught:
        _wait([iam.PollOutcome("token", TOKEN)], cancelled=lambda: flag["set"], clock=clock)
    assert caught.value.code == "cancelled"
    assert clock.now - 1000.0 == pytest.approx(1.0)  # not the full 6 s wait


def test_a_cancel_that_lands_while_the_poll_is_in_flight_still_cancels() -> None:
    """The commit boundary: the server said yes, the user said no, the user wins."""
    flag = {"set": False}

    def poll(endpoints: iam.Endpoints, device_code: str) -> iam.PollOutcome:
        flag["set"] = True  # Cancel pressed during the request
        return iam.PollOutcome("token", TOKEN)

    clock = Clock()
    with pytest.raises(iam.IamError) as caught:
        device_flow.wait_for_token(
            ENDPOINTS,
            GRANT,
            cancelled=lambda: flag["set"],
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            jitter=lambda a, b: 0.0,
            poll=poll,
        )
    assert caught.value.code == "cancelled"
    # The control: the same poll with no cancel hands the token back.
    token, _, _ = _wait([iam.PollOutcome("token", TOKEN)])
    assert token == TOKEN


# --- the commit boundary ------------------------------------------------------------------


class _Iam:
    """Scripted identity provider for ``commit_sign_in``: records what would have been written."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, previous: iam.Session | None) -> None:
        self.stored: list[dict[str, Any]] = []
        self.revoked: list[str] = []
        self.cancel = {"set": False}
        self.userinfo_calls = 0

        def fetch_userinfo(endpoints: iam.Endpoints, token: str) -> dict[str, Any]:
            self.userinfo_calls += 1
            self.cancel["set"] = self.cancel_during_userinfo
            return {"sub": "usr_1", "email": "new@example.com", "name": "New"}

        def store_session(**values: Any) -> iam.Session:
            self.stored.append(values)
            return iam.Session(
                api_url=values["api_url"], token=values["token"], source="file",
                email=values["claims"]["email"],
            )  # fmt: skip

        monkeypatch.setattr(iam, "fetch_userinfo", fetch_userinfo)
        monkeypatch.setattr(iam, "stored_session", lambda: previous)
        monkeypatch.setattr(iam, "store_session", store_session)

        def revoke(endpoints: iam.Endpoints, token: str) -> bool:
            self.revoked.append(token)
            return True

        monkeypatch.setattr(iam, "revoke", revoke)
        self.cancel_during_userinfo = False


def _previous(api_url: str = "https://api.example", token: str = "aisq_old") -> iam.Session:
    return iam.Session(api_url=api_url, token=token, source="file", email="old@example.com")


def test_commit_stores_the_session_and_retires_the_previous_token_on_the_same_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _Iam(monkeypatch, previous=_previous())

    session = device_flow.commit_sign_in(
        "https://api.example", ENDPOINTS, TOKEN, cancelled=lambda: fake.cancel["set"]
    )

    assert session.email == "new@example.com" and session.token == "aisq_new"
    [written] = fake.stored
    assert written["token"] == "aisq_new" and written["expires_in"] == 7776000
    assert written["scope"] == iam.SCOPE  # the response named none: the client's default
    assert fake.revoked == ["aisq_old"]


def test_commit_never_sends_a_token_to_another_hosts_revocation_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _Iam(monkeypatch, previous=_previous(api_url="https://other.example"))
    device_flow.commit_sign_in("https://api.example", ENDPOINTS, TOKEN, cancelled=lambda: False)
    assert fake.stored and fake.revoked == []  # stored, but the other host's token is left alone
    same = _Iam(monkeypatch, previous=_previous(token="aisq_new"))
    device_flow.commit_sign_in("https://api.example", ENDPOINTS, TOKEN, cancelled=lambda: False)
    assert same.revoked == []  # the same token is not "previous"


def test_a_cancel_during_the_userinfo_request_stores_nothing_and_revokes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The last request before the write: the server answered, the user had already said no."""
    fake = _Iam(monkeypatch, previous=_previous())
    fake.cancel_during_userinfo = True

    with pytest.raises(iam.IamError) as caught:
        device_flow.commit_sign_in(
            "https://api.example", ENDPOINTS, TOKEN, cancelled=lambda: fake.cancel["set"]
        )

    assert caught.value.code == "cancelled"
    assert fake.userinfo_calls == 1  # the request did complete…
    assert fake.stored == [] and fake.revoked == []  # …and changed nothing
