"""``paths.despite_windows_contention`` at every call site, simulated on any platform.

On NTFS a rename is refused while any other handle has the target open, even
only to read it, and for the width of that rename a reader takes an ``Access is
denied`` of its own. Each reader and writer here goes through the retry, and
before these tests none of them was held to it: reverting any wrapper left
every leg green (review of #65, R4). Here ``sys.platform`` says ``win32``, the
backoff is zero, and the first attempt fails the way Windows fails it.
``os.replace`` fails through the Win32 layer with ``winerror`` 5 or 32, and
``open`` through the C runtime with ``errno`` 13 and no ``winerror``. The
second attempt succeeds, so a caller without the retry fails or, worse, reads
"nothing there".
"""

from __future__ import annotations

import errno
import os
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from aisquare.core import credentials, paths
from aisquare.core.atomic import write_replacing
from aisquare.core.config import load_config, save_config


def _busy_read() -> PermissionError:
    """What ``open`` raises on Windows for a file mid-rename: errno 13, ``winerror`` None."""
    return PermissionError(errno.EACCES, "Permission denied")


class _Win32Error(PermissionError):
    """A ``PermissionError`` carrying ``winerror`` on any platform, as the Win32 layer raises it."""

    def __init__(self, winerror: int) -> None:
        super().__init__(errno.EACCES, "Access is denied")
        self.winerror = winerror


def _busy_replace(winerror: int = 5) -> PermissionError:
    """What ``os.replace`` raises on Windows over a file another handle holds."""
    return _Win32Error(winerror)


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """The retry's own branch, taken at once: Windows, and no sleep between attempts."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(paths, "_BUSY_BACKOFF_SECONDS", 0.0)


def _refuse_first_open(
    monkeypatch: pytest.MonkeyPatch, target: Path, method: str, mode: str | None = None
) -> list[str]:
    """Make the first ``Path.<method>`` of ``target`` (in ``mode``) fail as busy; record calls."""
    real: Callable[..., Any] = getattr(Path, method)
    calls: list[str] = []

    def once_busy(self: Path, *args: Any, **kwargs: Any) -> Any:
        opening = args[0] if args else kwargs.get("mode", "r")
        if self == target and (mode is None or opening == mode):
            calls.append(method)
            if len(calls) == 1:
                raise _busy_read()
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, method, once_busy)
    return calls


def test_credentials_read_while_busy_are_not_read_as_empty(
    isolated_home: Path, windows: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``load_all`` read a busy file as ``{}``, and ``store`` is read-merge-write: the next
    store kept only its own key, and the API key and serve token were gone."""
    credentials.store(api_key="k", serve_token="t")
    calls = _refuse_first_open(monkeypatch, paths.credentials_path(), "read_text")
    assert credentials.load_all() == {"api_key": "k", "serve_token": "t"}
    assert calls == ["read_text", "read_text"], "the busy read was not retried"


def test_a_config_read_while_busy_is_retried(
    tmp_path: Path, windows: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``cli/launch.py`` reads an unreadable config as "launch untraced", so a busy read that
    raised cost tracing with nothing said."""
    target = tmp_path / "config.toml"
    target.write_text('profile = "busy"\n', encoding="utf-8")
    calls = _refuse_first_open(monkeypatch, target, "open", "rb")
    assert load_config(target).profile == "busy"
    assert calls == ["open", "open"]


def test_a_save_keeps_unknown_keys_when_the_existing_file_is_busy(
    tmp_path: Path, windows: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fail-open read before a save swallowed a busy ``PermissionError`` (an ``OSError``)
    and skipped the unknown-key preservation: exit 0, keys another build wrote deleted."""
    target = tmp_path / "config.toml"
    target.write_text('profile = "default"\n\n[future_feature]\nsomething = 42\n')
    config = load_config(target)
    calls = _refuse_first_open(monkeypatch, target, "open", "rb")
    save_config(config, target)
    assert calls == ["open", "open"]
    with target.open("rb") as handle:
        assert tomllib.load(handle).get("future_feature") == {"something": 42}


@pytest.mark.parametrize("winerror", [5, 32], ids=["access-denied", "sharing-violation"])
def test_a_replace_refused_while_the_target_is_held_open_is_retried(
    tmp_path: Path, windows: None, monkeypatch: pytest.MonkeyPatch, winerror: int
) -> None:
    """Inside ``write_replacing``, so ``config.toml``, ``state.json``, the CI descriptor
    and the credentials all have it, not only the file whose writer was given it first."""
    target = tmp_path / "state.json"
    target.write_text("old\n")
    real_replace = os.replace
    attempts: list[int] = []

    def held_once(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        attempts.append(1)
        if len(attempts) == 1:
            raise _busy_replace(winerror)
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", held_once)
    write_replacing(target, "new\n")
    assert target.read_text() == "new\n"
    assert len(attempts) == 2


def test_a_genuine_refusal_is_raised_at_once_and_contention_only_after_the_last_try(
    windows: None,
) -> None:
    """``winerror`` 5 and 32 are "busy", and so is a bare errno 13 (the reader's shape).
    Anything else, like a privilege error or ``EPERM``, is raised on the first attempt.
    Busy that never clears is raised unchanged after ``_BUSY_ATTEMPTS``."""
    for busy in (_busy_replace(5), _busy_replace(32), _busy_read()):
        assert paths._is_contention(busy), busy
    not_busy = _busy_replace(1314)  # ERROR_PRIVILEGE_NOT_HELD
    assert not paths._is_contention(not_busy)
    assert not paths._is_contention(PermissionError(errno.EPERM, "Operation not permitted"))

    tries: list[int] = []

    def refuse(exc: PermissionError) -> Callable[[], None]:
        def action() -> None:
            tries.append(1)
            raise exc

        return action

    with pytest.raises(PermissionError) as raised:
        paths.despite_windows_contention(refuse(not_busy))
    assert raised.value is not_busy and len(tries) == 1

    tries.clear()
    held = _busy_replace(32)
    with pytest.raises(PermissionError) as raised:
        paths.despite_windows_contention(refuse(held))
    assert raised.value is held and len(tries) == paths._BUSY_ATTEMPTS
