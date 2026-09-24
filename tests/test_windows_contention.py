"""The NTFS contention retry, and the two reads whose correctness depends on it.

Both of those reads were fixed without a test. Nothing in the suite referenced
``despite_windows_contention`` or simulated the contention, the helper is a
pass-through off Windows, and the only Windows exercise was a config smoke test
that does not look at unknown-key preservation — so REVERTING either wrapper
left all five CI legs green. A fix whose removal nothing notices is a fix that
will be removed.

These run on every platform, because the retry keys on ``sys.platform`` rather
than on anything the kernel does: monkeypatch that and the Windows branch is
reachable from Linux. The contention itself is injected rather than raced for,
which also makes it deterministic instead of a 1-in-N.

Each test carries the REVERT as its control: the same scenario with the helper
neutered, asserting the old broken answer. Without that pair, a test that passes
proves only that nothing crashed.
"""

from __future__ import annotations

import errno
import json
import sys
import tomllib
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from aisquare.core import credentials, paths
from aisquare.core.config import AppConfig, save_config


def busy(*, winerror: int | None = None, err: int = errno.EACCES) -> PermissionError:
    """A ``PermissionError`` shaped like Windows saying "someone has this open".

    Set as an INSTANCE attribute, which is the only construction that works on
    both platforms: the four-argument ``PermissionError(errno, msg, name, 32)``
    form fills ``winerror`` on Windows and silently drops it on Linux, so a test
    written that way would assert nothing off Windows. Measured, both legs.

    ``winerror=None`` is the C-runtime shape ``Path.open`` raises — errno 13 and
    no ``winerror`` at all — which is the half a naive predicate misses.
    """
    exc = PermissionError(err, "Access is denied")
    if winerror is not None:
        # `object.__setattr__`, not `exc.winerror = …`: typeshed declares
        # `winerror` on Windows only, so the plain assignment needs a
        # `type: ignore[attr-defined]` on Linux and mypy calls that same
        # comment UNUSED on Windows. One spelling that type-checks on both
        # beats a platform-conditional comment.
        object.__setattr__(exc, "winerror", winerror)
    return exc


@pytest.fixture
def as_windows(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Take the Windows branch of the retry, and make its backoff free."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    yield


@pytest.fixture
def as_reverted(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """``despite_windows_contention`` as a pass-through — what a revert restores.

    Stated as a platform rather than left to the host. The first draft of these
    controls simply did not apply ``as_windows``, which reads as "the POSIX
    case" and is one on Linux — and on Windows it is not: the helper took the
    retry branch anyway and both controls failed. A control whose meaning
    depends on where it runs is not a control.
    """
    monkeypatch.setattr(sys, "platform", "linux")
    yield


def fail_once(
    monkeypatch: pytest.MonkeyPatch, target: Path, attribute: str, exc: OSError
) -> list[int]:
    """Make the FIRST ``Path.<attribute>`` call against ``target`` raise ``exc``.

    Returns the attempt log, so a test can assert the read was retried rather
    than merely that it eventually returned something.
    """
    attempts: list[int] = []
    real: Callable[..., Any] = getattr(Path, attribute)

    def flaky(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self == target:
            attempts.append(1)
            if len(attempts) == 1:
                raise exc
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, attribute, flaky)
    return attempts


# --------------------------------------------------------------- the predicate


@pytest.mark.parametrize(
    ("exc", "expected", "why"),
    [
        (busy(winerror=5), True, "ERROR_ACCESS_DENIED from the Win32 layer"),
        (busy(winerror=32), True, "ERROR_SHARING_VIOLATION from the Win32 layer"),
        (busy(winerror=2), False, "ERROR_FILE_NOT_FOUND is not contention"),
        (busy(), True, "errno 13 with no winerror — what Path.open raises"),
        (busy(err=errno.EPERM), False, "a genuine EPERM is not the busy shape"),
    ],
)
def test_is_contention_reads_both_shapes_windows_reports(
    exc: PermissionError, expected: bool, why: str
) -> None:
    """Both reporting shapes, because matching only one disables half the retry."""
    assert paths._is_contention(exc) is expected, why


# ------------------------------------------------- credentials.load_all (65-B1)


def _write_credentials() -> Path:
    path = paths.credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"api_key": "key-1", "serve_token": "tok-1"}), encoding="utf-8")
    return path


def test_load_all_retries_a_busy_read_rather_than_reading_the_file_as_empty(
    as_windows: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A racing read must not be reported as "nothing stored".

    ``store()`` is read-merge-write over a whole-file write, so ``{}`` here
    means the next write erases whatever it did not see — the API key, the serve
    token or the IAM session. This module's own header calls that out: "'empty'
    is the exact reading that lost data."
    """
    path = _write_credentials()
    attempts = fail_once(monkeypatch, path, "read_text", busy())

    assert credentials.load_all() == {"api_key": "key-1", "serve_token": "tok-1"}
    assert len(attempts) == 2, ("the read was not retried", attempts)


def test_without_the_retry_a_busy_read_loses_every_secret_in_the_file(
    as_reverted: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: what a revert looks like.

    With the helper as a pass-through, the ``except OSError`` below it reads the
    contention as an empty file. If this ever returns the secrets, the test
    above has stopped proving anything.
    """
    path = _write_credentials()
    fail_once(monkeypatch, path, "read_text", busy())

    assert credentials.load_all() == {}


# ---------------------------------------------------- config.save_config (65-B2)


def _config_with_an_unknown_key() -> Path:
    path = paths.config_path()
    save_config(AppConfig())
    text = path.read_text(encoding="utf-8")
    path.write_text(f'{text}\n[section_from_a_newer_build]\nkeep = "me"\n', encoding="utf-8")
    return path


def test_save_config_keeps_unknown_keys_when_the_read_back_is_busy(
    as_windows: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A busy read-back must not silently drop a newer build's config.

    ``_keep_unknown`` names the cost: "exit 0, no warning, and because the
    tracing seam is fail-open the result is a green-looking machine with no
    tracing". Failing open is right for a file we cannot PARSE and wrong for one
    that is busy for 40 microseconds.
    """
    path = _config_with_an_unknown_key()
    attempts = fail_once(monkeypatch, path, "open", busy())

    save_config(AppConfig())

    survived = tomllib.loads(path.read_text(encoding="utf-8"))
    assert survived.get("section_from_a_newer_build") == {"keep": "me"}
    assert len(attempts) >= 2, ("the read-back was not retried", attempts)


def test_without_the_retry_a_busy_read_back_drops_the_unknown_key(
    as_reverted: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control, as above: with the helper a pass-through.

    `contextlib.suppress(OSError, ...)` then swallows the contention and the
    write proceeds without the preservation step — exit 0, key gone.
    """
    path = _config_with_an_unknown_key()
    fail_once(monkeypatch, path, "open", busy())

    save_config(AppConfig())

    survived = tomllib.loads(path.read_text(encoding="utf-8"))
    assert "section_from_a_newer_build" not in survived
