"""Two filesystem capabilities the suite asserts against, per platform.

Both exist because a POSIX idiom used as a test PREMISE silently stops being
one on Windows — and a premise that quietly fails does not fail the test, it
makes the test pass for the wrong reason (or fail for a reason that has nothing
to do with the code under test).

``unwritable`` is the sharper of the two. ``os.chmod(dir, 0o500)`` is a no-op
against a directory on Windows: Python maps the mode bits onto the single
read-only FILE attribute, which directories ignore, so ``chmod`` returns
cleanly and the directory stays writable. Measured, not assumed — a write into
a 0o500 directory succeeds there. Tests that manufacture "this write must fail"
that way were therefore asserting nothing on Windows. An explicit DENY ace does
what the mode bits do on POSIX, and Python raises the same ``PermissionError``
with the same ``errno`` (13) through it, so the assertions on the other side
need no platform branch at all.

``can_symlink`` is a CAPABILITY probe, not a platform check, and that
distinction is the point. Creating a symlink on Windows needs
SeCreateSymbolicLinkPrivilege, which an administrator or a machine in Developer
Mode holds and an ordinary account does not — so the same code passes on the
CI runner (``runneradmin``) and fails on a developer's box. Probing what this
machine can actually do keeps the answer honest in both places.
"""

from __future__ import annotations

import getpass
import os
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

#: Deny the write-ish rights only. WD (write data / create files), AD (append
#: data / create subdirectories) and WA (write attributes) are what a directory
#: needs to accept a new file; DELETE is deliberately NOT denied, so pytest can
#: still clean the tmp tree up if a test dies before its `finally`.
_DENY_RIGHTS = "(WD,AD,WA)"


def _icacls(path: Path, *args: str) -> bool:
    try:
        result = subprocess.run(
            ["icacls", str(path), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):  # pragma: no cover - platform-dependent
        return False
    return result.returncode == 0


def _write_is_refused(directory: Path) -> bool:
    """Whether a new file in ``directory`` actually fails right now."""
    probe = directory / ".aisquare-write-probe"
    try:
        probe.write_text("probe", encoding="utf-8")
    except OSError:
        return True
    probe.unlink(missing_ok=True)
    return False


@contextmanager
def _applied(directory: Path) -> Iterator[None]:
    """Apply the platform's denial to ``directory`` and undo it afterwards."""
    if sys.platform != "win32":
        original = stat.S_IMODE(directory.stat().st_mode)
        os.chmod(directory, 0o500)
        try:
            yield
        finally:
            os.chmod(directory, original)
        return

    user = getpass.getuser()
    if not _icacls(directory, "/deny", f"{user}:{_DENY_RIGHTS}"):  # pragma: no cover
        raise RuntimeError(f"could not deny writes on {directory} via icacls")
    try:
        yield
    finally:
        # `/remove:d` drops the DENY entry specifically, leaving any pre-existing
        # ALLOW entry for the same user in place.
        _icacls(directory, "/remove:d", user)


@contextmanager
def unwritable(directory: Path) -> Iterator[Path]:
    """Make ``directory`` reject new files for the duration, then restore it.

    The denial is VERIFIED by attempting a write, not assumed from the fact that
    the syscall returned. That check earns its place twice over:

    * ``chmod`` against a Windows directory returns cleanly and denies nothing,
      which is the defect this helper exists for;
    * and ROOT bypasses the mode bits entirely, so in a container running as
      root — the default for ``docker run`` — a 0o500 directory stays perfectly
      writable and every "this must fail" test passes for the wrong reason.

    Raises ``RuntimeError`` rather than yielding when the premise did not take.
    Use :func:`can_deny_writes` to skip instead of failing where it cannot.
    """
    with _applied(directory):
        if not _write_is_refused(directory):
            raise RuntimeError(
                f"could not make {directory} unwritable — the restriction was applied "
                "but a write still succeeded (running as root?). Guard the caller "
                "with can_deny_writes()."
            )
        yield directory


def can_deny_writes() -> bool:
    """Whether ``unwritable`` can actually deny a write on this machine.

    False when running as root, where the mode bits are advisory. CI runs as an
    ordinary user, so the tests this guards still run there; a root container
    skips them rather than reporting a denial that never happened.
    """
    with tempfile.TemporaryDirectory() as raw:
        probe = Path(raw) / "vault"
        probe.mkdir()
        try:
            with unwritable(probe):
                return True
        except (RuntimeError, OSError):
            return False


def can_symlink() -> bool:
    """Whether THIS machine can create a symlink, measured by creating one."""
    with tempfile.TemporaryDirectory() as raw:
        base = Path(raw)
        target = base / "target"
        target.write_text("probe", encoding="utf-8")
        try:
            (base / "link").symlink_to(target)
        except (OSError, NotImplementedError):
            return False
        return True
