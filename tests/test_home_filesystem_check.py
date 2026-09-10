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

import sys
from pathlib import Path

import pytest

from aisquare.models import CheckStatus
from aisquare.services import diagnostics
from tests.fsperms import can_symlink

# Two real lines from this machine's /proc/self/mountinfo, trimmed to the fields
# the parser reads. Using the real shape matters: the fstype sits AFTER the
# " - " separator, and the optional fields before it are variable-length, which
# is the thing a hand-invented fixture gets wrong.
MOUNTINFO = """\
24 30 0:22 / / rw,relatime shared:1 - ext4 /dev/sdd rw,discard,errors=remount-ro
30 24 0:24 / /mnt/c rw,noatime - 9p drvfs rw,aname=drvfs;path=C:\\;uid=1001,trans=fd
31 24 0:25 / /mnt/c/nested rw,noatime - ext4 /dev/loop0 rw
"""


# The mount table is a POSIX artefact and so are the paths inside it: matching a
# mount point against a target needs POSIX path semantics, and on Windows
# `Path("/mnt/c/nested/x").resolve()` becomes `<drive>:\mnt\c\nested\x`, which is
# no longer under `Path("/mnt/c/nested")`. The product is already right there —
# `/proc/self/mountinfo` does not exist, so `filesystem_of` returns the honest
# None through its fail-open path, which the Windows test at the bottom pins.
_parses_mountinfo = pytest.mark.skipif(
    sys.platform == "win32", reason="mountinfo matching needs POSIX path semantics"
)
_needs_symlink = pytest.mark.skipif(
    not can_symlink(), reason="this machine cannot create symlinks (needs privilege on Windows)"
)


@pytest.fixture
def mountinfo(tmp_path: Path) -> Path:
    path = tmp_path / "mountinfo"
    path.write_text(MOUNTINFO, encoding="utf-8")
    return path


@_parses_mountinfo
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


def test_the_line_reports_a_plain_config_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """@9bbc8ed7's extension, accepted into this line rather than a new check.

    Whether the config is a symlink is the same class of fact as which
    filesystem it is on: invisible, chosen by the user, and consequential to how
    a write behaves. Since ``save_config`` follows links, this is also the line
    that tells an operator their dotfiles link IS being honoured — reassuring
    only once it is visible.
    """
    monkeypatch.setenv("AISQUARE_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text("profile = 'default'\n", encoding="utf-8")

    assert "regular file" in diagnostics._check_home_filesystem().detail


@_needs_symlink
def test_the_line_names_what_a_symlinked_config_points_at(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Naming the destination is the point: "symlink" alone says it is not a file."""
    dotfiles = tmp_path / "dotfiles"
    home = tmp_path / "home"
    dotfiles.mkdir()
    home.mkdir()
    real = dotfiles / "config.toml"
    real.write_text("profile = 'default'\n", encoding="utf-8")
    (home / "config.toml").symlink_to(real)
    monkeypatch.setenv("AISQUARE_HOME", str(home))

    detail = diagnostics._check_home_filesystem().detail

    assert "symlink" in detail
    assert str(real) in detail, "the line must say WHERE the link points"


def test_a_missing_config_is_said_plainly(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Before `init`, and it must not read as a problem — `home`/`config` own that."""
    monkeypatch.setenv("AISQUARE_HOME", str(tmp_path))

    check = diagnostics._check_home_filesystem()

    assert check.status is CheckStatus.ok
    assert "not created yet" in check.detail


def test_the_file_kind_never_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """It runs on every `doctor`; a stat failure must not take the command down."""
    monkeypatch.setenv("AISQUARE_HOME", str(tmp_path))

    def _boom(self: Path) -> bool:
        raise OSError("stat refused")

    monkeypatch.setattr(Path, "is_symlink", _boom)

    assert diagnostics._config_file_kind() == "unreadable"


@_needs_symlink
def test_a_dangling_symlink_target_is_flagged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The one symlink shape whose consequences reach outside AISQUARE_HOME.

    ``save_config`` follows a broken link and CREATES the missing directories at
    its target — measured at four levels deep, and on a mounted Windows drive if
    that is where the link points. @9bbc8ed7 raised that as a boundary this team
    has held by hand four times, now reachable by the product through a link a
    user set and forgot.

    The behaviour stands (@dfd9a883's ruling: failing on a broken link would add
    a failure mode in a state that is the user's to fix). What it lacked was
    anyone being able to SEE it, which is what this line is for. Still a
    detector — flagged in the detail, not promoted to a failure.
    """
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").symlink_to(tmp_path / "never" / "cloned" / "config.toml")
    monkeypatch.setenv("AISQUARE_HOME", str(home))

    check = diagnostics._check_home_filesystem()

    assert "TARGET MISSING" in check.detail, (
        "a dangling config link is invisible again — the state that lets a write "
        "create directories somewhere nobody named"
    )
    assert check.status is not CheckStatus.fail, "a detector must not fail the machine"


@_needs_symlink
def test_a_live_symlink_is_not_flagged_as_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The negative half: a working link must read as reassurance, not a warning."""
    dotfiles = tmp_path / "dotfiles"
    home = tmp_path / "home"
    dotfiles.mkdir()
    home.mkdir()
    real = dotfiles / "config.toml"
    real.write_text("profile = 'default'\n", encoding="utf-8")
    (home / "config.toml").symlink_to(real)
    monkeypatch.setenv("AISQUARE_HOME", str(home))

    detail = diagnostics._check_home_filesystem().detail

    assert "symlink" in detail
    assert "TARGET MISSING" not in detail


@pytest.mark.parametrize(
    ("shape", "expected"),
    [("regular file", "config: regular file"), ("not created yet", "config: not created yet")],
)
def test_every_shape_reads_as_a_sentence(
    monkeypatch: pytest.MonkeyPatch, shape: str, expected: str
) -> None:
    """The third shape made the line ungrammatical, and it is the first-run one.

    The detail interpolated "config is a {shape}", which reads correctly for
    "regular file" and "symlink -> …" and produces "config is a not created yet"
    for the third — the state a FRESH machine is in, so the only one a first-time
    operator is guaranteed to see. @9bbc8ed7 found it and declined to fix wording
    on someone else's line mid-shift, which was the right call and is why it
    reached me rather than being changed quietly.

    Parametrised over the shapes rather than asserting one, because the defect
    was that two of three read correctly — a single-case test would have passed
    on the wording that was already fine.
    """
    monkeypatch.setattr(diagnostics, "_config_file_kind", lambda: shape)
    monkeypatch.setattr(diagnostics, "filesystem_of", lambda path, *_: "ext4")

    detail = diagnostics._check_home_filesystem().detail

    assert expected in detail
    assert "config is a" not in detail, "the ungrammatical form is back"


def test_the_warn_branch_reads_as_a_sentence_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """Four call sites interpolate the shape; three were fixed by one pattern.

    The fourth is the translated-filesystem warning, whose wording differs
    enough that a search-and-replace over the other three left it behind — which
    is exactly how a partial fix survives review.
    """
    monkeypatch.setattr(diagnostics, "_config_file_kind", lambda: "not created yet")
    monkeypatch.setattr(diagnostics, "filesystem_of", lambda path, *_: "9p")

    check = diagnostics._check_home_filesystem()

    assert check.status is CheckStatus.warn
    assert "config is a" not in check.detail, "the warn branch kept the old wording"


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows answer")
def test_the_unknown_answer_is_what_windows_gets() -> None:
    """Windows has no mount table, and `filesystem_of` must say so rather than guess.

    The parser test above skips here, so without this the Windows behaviour of
    the function would be asserted nowhere at all. Unknown is the honest answer
    and the caller already renders it as one — what must never happen is a
    raise, because this feeds `doctor`.
    """
    assert diagnostics.filesystem_of(Path.home() / ".aisquare") is None
