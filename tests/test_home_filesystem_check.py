"""`doctor` should say which filesystem the config lives on.

``save_config`` publishes changes with write-temp / fsync / rename / fsync-parent.
The atomicity of that rename belongs to the FILESYSTEM, and this project has only
ever measured it on a native disk. ``AISQUARE_HOME`` is taken verbatim, this box
has six Windows-backed 9p mounts, and the caveat recorded at
``core.paths.aisquare_home`` ended with the honest admission that "nothing in the
code can tell you which kind of path it is on".

Now it can. @dfd9a883's shape: the useful product of the unmeasured 9p question
is not a number, it is a line the operator can read — a DETECTOR that asks for a
decision rather than a checker that mandates one.

What is deliberately NOT done: being on a translated filesystem never fails the
check. It is unmeasured, not known-broken, and turning one into the other inside
a tool is the mandate this project has repeatedly declined to write.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aisquare.models import CheckStatus
from aisquare.services import diagnostics

# Two real lines from this machine's /proc/self/mountinfo, trimmed to the fields
# the parser reads. Using the real shape matters: the fstype sits AFTER the
# " - " separator, and the optional fields before it are variable-length, which
# is the thing a hand-invented fixture gets wrong.
MOUNTINFO = """\
24 30 0:22 / / rw,relatime shared:1 - ext4 /dev/sdd rw,discard,errors=remount-ro
30 24 0:24 / /mnt/c rw,noatime - 9p drvfs rw,aname=drvfs;path=C:\\;uid=1001,trans=fd
31 24 0:25 / /mnt/c/nested rw,noatime - ext4 /dev/loop0 rw
"""


@pytest.fixture
def mountinfo(tmp_path: Path) -> Path:
    path = tmp_path / "mountinfo"
    path.write_text(MOUNTINFO, encoding="utf-8")
    return path


def test_the_longest_mount_point_wins(mountinfo: Path) -> None:
    """Mounts nest, so the first match is not the right one.

    ``/mnt/c/nested`` is mounted under ``/mnt/c``. Taking the first prefix match
    would report 9p for a path that is actually on ext4 — a wrong answer that
    looks plausible, which is worse than no answer.
    """
    assert diagnostics.filesystem_of(Path("/mnt/c/nested/x"), mountinfo) == "ext4"
    assert diagnostics.filesystem_of(Path("/mnt/c/other/x"), mountinfo) == "9p"
    assert diagnostics.filesystem_of(Path("/home/work/.aisquare"), mountinfo) == "ext4"


def test_an_unreadable_mountinfo_is_unknown_rather_than_an_error(tmp_path: Path) -> None:
    """Fail open: a diagnostic must not be the thing that breaks a machine.

    macOS has no /proc at all, and a container may have an unreadable one.
    """
    assert diagnostics.filesystem_of(Path("/anywhere"), tmp_path / "does-not-exist") is None


def test_a_native_filesystem_reads_as_settled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(diagnostics, "filesystem_of", lambda path, *_: "ext4")

    check = diagnostics._check_home_filesystem()

    assert check.status is CheckStatus.ok
    assert "ext4" in check.detail


def test_a_translated_filesystem_warns_and_says_why(monkeypatch: pytest.MonkeyPatch) -> None:
    """The line that did not exist before, and the reason the task was filed."""
    monkeypatch.setattr(diagnostics, "filesystem_of", lambda path, *_: "9p")

    check = diagnostics._check_home_filesystem()

    assert check.status is CheckStatus.warn
    assert "9p" in check.detail
    assert "unverified" in check.detail
    assert "AISQUARE_HOME" in (check.fix or ""), "the fix must name the knob that moves it"


def test_an_unknown_filesystem_does_not_warn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never seen is not the same as suspect.

    A filesystem absent from both lists gets a factual line and an ok status.
    Warning on everything unrecognised would make the check noise, and a noisy
    detector is one people learn to skip past.
    """
    monkeypatch.setattr(diagnostics, "filesystem_of", lambda path, *_: "someothersfs")

    check = diagnostics._check_home_filesystem()

    assert check.status is CheckStatus.ok
    assert "someothersfs" in check.detail


def test_it_never_fails_the_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    """No filesystem answer may produce a `fail`.

    `doctor` exits non-zero on a failing check, and where the config happens to
    live must never be the reason a machine reports broken.
    """
    for kind in ("ext4", "9p", "cifs", "wat", None):
        monkeypatch.setattr(diagnostics, "filesystem_of", lambda path, *_, k=kind: k)
        assert diagnostics._check_home_filesystem().status is not CheckStatus.fail


def test_the_check_is_in_the_doctor_run() -> None:
    """A check nobody runs is a function, not a diagnostic."""
    names = [check.name for check in diagnostics.doctor()]

    assert "filesystem" in names
    assert names.index("filesystem") == names.index("home") + 1, (
        "the filesystem line belongs beside the home line it qualifies"
    )
