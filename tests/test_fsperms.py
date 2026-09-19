"""The premise-manufacturing helpers, asserted from both ends.

``unwritable`` is now the premise of five tests that require a write to FAIL.
A premise helper that silently stops working does not fail those tests — it
makes them pass for the wrong reason, which is the exact defect it was written
to repair (``chmod(0o500)`` on a Windows directory returns cleanly and denies
nothing). So it is tested the same way the daemon probe is: it must really deny
while it is applied, and it must really restore afterwards.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from tests.fsperms import can_deny_writes, can_symlink, unwritable

# Root bypasses the mode bits, so a container running as root cannot deny a
# write at all. CI runs as an ordinary user and still asserts all of this.
_can_deny = pytest.mark.skipif(
    not can_deny_writes(), reason="writes cannot be denied here (running as root?)"
)


@_can_deny
def test_the_directory_really_rejects_a_write_while_denied(tmp_path: Path) -> None:
    """The load-bearing half: the write must actually fail."""
    vault = tmp_path / "vault"
    vault.mkdir()

    with unwritable(vault), pytest.raises(PermissionError):
        (vault / "probe.txt").write_text("nope", encoding="utf-8")


@_can_deny
def test_the_denial_is_lifted_afterwards(tmp_path: Path) -> None:
    """And the other half: a test that leaked a DENY ace would poison the tmp tree."""
    vault = tmp_path / "vault"
    vault.mkdir()

    with unwritable(vault):
        pass

    (vault / "probe.txt").write_text("fine", encoding="utf-8")
    assert (vault / "probe.txt").read_text(encoding="utf-8") == "fine"


@_can_deny
def test_the_denial_is_lifted_even_when_the_body_raises(tmp_path: Path) -> None:
    """The `finally` is what keeps a failing test from breaking the next one."""
    vault = tmp_path / "vault"
    vault.mkdir()

    with pytest.raises(RuntimeError, match="deliberate"), unwritable(vault):
        raise RuntimeError("deliberate")

    (vault / "probe.txt").write_text("fine", encoding="utf-8")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
@_can_deny
def test_the_original_mode_comes_back_on_posix(tmp_path: Path) -> None:
    """Restoring to a hardcoded 0o700 would silently widen a stricter directory."""
    vault = tmp_path / "vault"
    vault.mkdir()
    os.chmod(vault, 0o750)

    with unwritable(vault):
        pass

    assert stat.S_IMODE(vault.stat().st_mode) == 0o750


def test_can_symlink_agrees_with_what_actually_happens(tmp_path: Path) -> None:
    """The probe must match reality, or it skips tests that would have run."""
    target = tmp_path / "target"
    target.write_text("x", encoding="utf-8")
    try:
        (tmp_path / "link").symlink_to(target)
    except (OSError, NotImplementedError):
        assert not can_symlink(), "the probe claims symlinks work where they do not"
    else:
        assert can_symlink(), "the probe claims symlinks fail where they work"
