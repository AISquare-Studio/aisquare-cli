"""Every reason an account has no usage to show has a cause, a reason and a fix (#13).

The owner: an unreadable account shows one ``!`` on the Accounts page, and hovering it
says what is wrong in plain words and what to do about it, if anything. So every
reading with nothing to show carries a :class:`UsageCause`, stamped where the reading is
made (``services.claude_accounts.unreadable_usage``), and the fix comes from one table
(``USAGE_FIXES``), never from the reason's words. The table below is that contract, one
row per cause: a cause added without its row fails here first. Each row is produced the
way the service produces it (a fetch that answers 401, a Keychain that says locked…),
and read back as the page's tooltip and as ``accounts usage``'s cell say it.
"""

from __future__ import annotations

import ssl
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from urllib.error import URLError

import pytest

from aisquare.cli.ui.views.accounts import problem_tooltip
from aisquare.core import claude_accounts as core
from aisquare.core import tls
from aisquare.models import ClaudeAccount, ClaudeUsage, UsageCause
from aisquare.services import claude_accounts as service
from tests.test_claude_accounts import NOW, _sign_in

_LOG_IN = "press Log in (in a terminal: aisquare accounts run 2, then /login)"
_REFRESH_TOKEN = (
    "press Log in, or open a session on this account (aisquare accounts run 2): "
    "Claude Code refreshes the token"
)
_THEN_REFRESH = "then ↻ Refresh usage (or --refresh)"
_RESUMES = (NOW + timedelta(seconds=120)).astimezone().strftime("%H:%M")

#: Every cause: the reason it reads with, and its fix — ``None`` where nothing done on this
#: machine helps, which the tooltip and ``accounts usage`` then say (``NO_FIX``).
CAUSES: dict[UsageCause, tuple[str, str | None]] = {
    UsageCause.not_signed_in: ("not signed in", _LOG_IN),
    UsageCause.token_missing: ("no credentials file — not signed in", _LOG_IN),
    UsageCause.token_expired: ("the stored token has expired", _REFRESH_TOKEN),
    UsageCause.token_rejected: ("the token was rejected (HTTP 401)", _REFRESH_TOKEN),
    UsageCause.keychain_locked: (
        "the login Keychain is locked",
        f"unlock the login keychain (Keychain Access, or log out and back in), {_THEN_REFRESH}",
    ),
    UsageCause.keychain_refused: (
        "macOS refused aisquare access to the Claude Code item",
        f"allow aisquare in Keychain Access, {_THEN_REFRESH}",
    ),
    UsageCause.keychain_timeout: (
        "could not read the Keychain (no answer in 5 s)",
        f"answer the Keychain's prompt if one is waiting, {_THEN_REFRESH}",
    ),
    UsageCause.keychain_failed: (
        "could not read the Keychain (security exited 25)",
        "↻ Refresh usage (or --refresh) asks the Keychain again",
    ),
    UsageCause.rate_limited: (
        "HTTP 429 from the usage endpoint — rate-limited",
        f"none needed, reads resume at {_RESUMES} by themselves",
    ),
    UsageCause.unreachable: (
        "could not reach the usage endpoint (ConnectionRefusedError)",
        "check the network or HTTPS_PROXY",
    ),
    # #403's wording: its reason, then its fix, the words doctor's tls row says too.
    UsageCause.tls_bundle: ("TLS: the CA bundle could not be loaded", tls.BUNDLE_FIX),
    UsageCause.tls_cert: ("TLS certificate could not be verified", tls.CERT_HINT),
    UsageCause.http_error: ("HTTP 500 from the usage endpoint", None),
    UsageCause.bad_answer: ("the usage endpoint did not answer with JSON", None),
    UsageCause.read_failed: ("usage read failed (RuntimeError)", None),
}

Answer = tuple[int, bytes] | tuple[int, bytes, Mapping[str, str]]


def _answering(answer: Answer | BaseException) -> Callable[..., Answer]:
    def fetch(url: str, headers: Mapping[str, str], timeout: float) -> Answer:
        if isinstance(answer, BaseException):
            raise answer
        return answer

    return fetch


def _keychain(monkeypatch: pytest.MonkeyPatch, problem: core.KeychainProblem, said: str) -> None:
    """A Keychain that would not say, as ``core.stored_login`` answers for it."""
    monkeypatch.setattr(
        core, "stored_login", lambda account, fresh=False: core.StoredLogin(None, said, problem)
    )


def _reading(
    cause: UsageCause, account: ClaudeAccount, monkeypatch: pytest.MonkeyPatch
) -> ClaudeUsage:
    """``cause``, produced the way the service produces it for ``account`` (slot 2)."""

    def read(answer: Answer | BaseException) -> ClaudeUsage:
        return service.usage(account, now=NOW, fetch=_answering(answer))

    if cause is UsageCause.not_signed_in:  # read from the row's state: nothing is asked
        problem = service.usage_problem(service.describe(account), None)
        assert problem is not None
        return problem
    if cause is UsageCause.token_missing:
        _sign_in(account, "two@example.com", credentials=False)
        return read((200, b"{}"))
    if cause is UsageCause.token_expired:
        _sign_in(account, "two@example.com", expires_in=timedelta(hours=-1))
        return read((200, b"{}"))
    _sign_in(account, "two@example.com")
    if cause is UsageCause.keychain_locked:
        _keychain(monkeypatch, "locked", core.KEYCHAIN_LOCKED)
    elif cause is UsageCause.keychain_refused:
        _keychain(monkeypatch, "refused", core.KEYCHAIN_REFUSED)
    elif cause is UsageCause.keychain_timeout:
        _keychain(monkeypatch, "timeout", "could not read the Keychain (no answer in 5 s)")
    elif cause is UsageCause.keychain_failed:
        _keychain(monkeypatch, "failed", "could not read the Keychain (security exited 25)")
    if cause.name.startswith("keychain_"):
        return read((200, b"{}"))
    if cause is UsageCause.tls_bundle:  # #403: SSL_CERT_FILE names a bundle that will not load
        return read(tls.CABundleError("SSL_CERT_FILE=/dev/null: no certificate in it"))
    if cause is UsageCause.tls_cert:  # #403: a proxy re-signs HTTPS with a CA not trusted
        return read(URLError(ssl.SSLCertVerificationError(1, "certificate verify failed")))
    if cause is UsageCause.read_failed:  # not an OSError: `read_usage` keeps the round going
        fetch = _answering(RuntimeError("the endpoint's shape broke the reader"))
        return service.read_usage([account], now=NOW, fetch=fetch)[account.slot]
    answers: dict[UsageCause, Answer | BaseException] = {
        UsageCause.token_rejected: (401, b""),
        UsageCause.rate_limited: (429, b"", {"Retry-After": "120"}),
        UsageCause.unreachable: ConnectionRefusedError(61, "refused"),
        UsageCause.http_error: (500, b""),
        UsageCause.bad_answer: (200, b"<html>"),
    }
    return read(answers[cause])


def test_every_cause_has_a_row_here_and_in_the_one_table_of_fixes() -> None:
    assert set(CAUSES) == set(UsageCause), "a cause with no reason or fix said for it"
    assert set(service.USAGE_FIXES) == set(UsageCause)


@pytest.mark.parametrize("cause", list(UsageCause), ids=[cause.value for cause in UsageCause])
def test_every_cause_reads_with_its_reason_and_its_fix_or_says_there_is_none(
    cause: UsageCause, fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = core.create_account()
    reason, fix = CAUSES[cause]
    reading = _reading(cause, account, monkeypatch)

    assert not reading.available
    assert (reading.cause, reading.reason, reading.fix) == (cause, reason, fix)
    said = f"fix: {fix}" if fix is not None else service.NO_FIX
    assert said.startswith("fix: ")
    # The Accounts page's '!', on hover: what is wrong, then what to do (or that nothing does).
    status = service.describe(account)
    # A rate limit names its wait as of the paint, not the reason frozen at the read (#401 m1).
    shown = reason
    if cause is UsageCause.rate_limited:
        assert reading.rate_limited_until is not None
        shown = service.backoff_reason(reading.rate_limited_until)
    assert problem_tooltip(status, reading, now=NOW) == f"{shown}\n{said}"
    # `accounts list --usage` and `accounts usage`: no hover, so the two on one line.
    assert service.explain_usage(reading) == f"{reason}; {said}"


def test_a_rate_limits_fix_says_when_reads_resume_and_without_a_time_that_they_do() -> None:
    until = datetime(2026, 9, 9, 14, 32).astimezone()
    assert service.usage_fix(UsageCause.rate_limited, until=until) == (
        "none needed, reads resume at 14:32 by themselves"
    )
    assert service.usage_fix(UsageCause.rate_limited) == "none needed, reads resume by themselves"
    # A wait being held says the same, from the wait's own end (F1 of #400's back-off).
    held = service.UsageBackoff(slot=2, until=until, attempts=1, since=until - timedelta(minutes=1))
    assert held.reading(until - timedelta(seconds=30)).fix == (
        "none needed, reads resume at 14:32 by themselves"
    )


def test_a_fix_names_the_slot_it_is_for_and_a_placeholder_without_one() -> None:
    assert service.usage_fix(UsageCause.token_missing, slot=3) == (
        "press Log in (in a terminal: aisquare accounts run 3, then /login)"
    )
    assert "aisquare accounts run <slot>" in (service.usage_fix(UsageCause.token_missing) or "")
