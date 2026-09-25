"""``paths.restrict_to_owner`` -- the guard on the API key and the serve token."""

from __future__ import annotations

import stat
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from aisquare.core import paths


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_restrict_to_owner_sets_0600_on_posix(tmp_path: Path) -> None:
    secret = tmp_path / "credentials"
    secret.write_text("token", encoding="utf-8")
    assert paths.restrict_to_owner(secret) is True
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS ACLs")
def test_restrict_to_owner_removes_a_broad_grant_on_windows(tmp_path: Path) -> None:
    """The load-bearing assertion: a real leak is really removed.

    Asserting only "no broad principal is present" would pass vacuously on any
    machine whose temp directory already sits inside the user's profile -- true
    of a developer box, false of a CI runner. Granting BUILTIN\\Users first
    makes the file genuinely readable by every account on the machine, so the
    assertion afterwards can only pass if restrict_to_owner did the work.
    """
    from tests import winacl

    secret = tmp_path / "credentials"
    secret.write_text("token", encoding="utf-8")

    winacl.grant_users_group(secret)
    assert winacl.USERS_TRUSTEE in winacl.dacl_trustees(secret), "the leak was not manufactured"

    assert paths.restrict_to_owner(secret) is True

    trustees = winacl.dacl_trustees(secret)
    me = winacl.current_user_sid()
    mine = winacl.user_trustees(trustees, me)
    assert winacl.USERS_TRUSTEE not in trustees, trustees
    assert mine, (trustees, me)  # the owner must keep access, or the file is useless
    assert not (trustees - winacl.PRIVILEGED_TRUSTEES - mine), trustees


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS ACLs")
def test_restrict_to_owner_names_three_principals_and_not_a_fourth(tmp_path: Path) -> None:
    """The LIMIT of the current approach, pinned so it is a decision, not a surprise.

    `restrict_to_owner` used to run `icacls /reset` first, which discards EVERY
    explicit ACE. It no longer does: `/reset` restores inheritance from the
    parent, so between that call and the grant that followed it the file sat on
    the parent's DACL with the secret already written — and a failure of the
    second call left the file wider than before it was called.

    The single call that replaced the pair cannot half-apply, but `/remove`
    drops only the principals it names. This test grants a FOURTH — one the
    argv does not mention — and asserts it survives. That is not the behaviour
    anyone wants; it is the behaviour we have, and a suite that only ever
    manufactured the Users group would report the narrowing as success.

    What changed is the ANSWER. The DACL is read back after the call, so the
    surviving grant makes `restrict_to_owner` return False, and its callers
    warn instead of promising an owner-only file (review of #65, R3).
    """
    from tests import winacl

    secret = tmp_path / "credentials"
    secret.write_text("token", encoding="utf-8")

    winacl.grant_users_group(secret)
    winacl.grant_other_principal(secret)
    before = winacl.dacl_trustees(secret)
    assert winacl.USERS_TRUSTEE in before, "the named leak was not manufactured"
    assert winacl.OTHER_PRINCIPAL_TRUSTEE in before, "the unnamed grant was not manufactured"

    assert paths.restrict_to_owner(secret) is False, "a surviving grant was reported as owner-only"

    after = winacl.dacl_trustees(secret)
    assert winacl.USERS_TRUSTEE not in after, ("a named principal survived", after)
    assert winacl.OTHER_PRINCIPAL_TRUSTEE in after, (
        "an UNNAMED principal was removed — the implementation grew beyond the "
        "three SIDs in its argv, and this test's whole subject has changed",
        after,
    )


def test_sddl_abbreviations_resolve_to_the_current_account() -> None:
    """SDDL writes some account SIDs as abbreviations, depending who is logged in.

    A CI runner logs in as the built-in Administrator, whose SID comes back as
    ``LA``; a desktop login has a RID well above 500 and is spelled out. This
    ran green locally and red on the runner until the two were reconciled, so
    the mapping is asserted here rather than left to whichever host runs it.
    """
    from tests import winacl

    admin = "S-1-5-21-286213758-782762298-1154913829-500"
    ordinary = "S-1-5-21-219110720-4157673820-4089457075-1001"

    assert winacl.denotes_user("LA", admin)
    assert not winacl.denotes_user("LA", ordinary)
    assert winacl.denotes_user(ordinary, ordinary)
    assert not winacl.denotes_user("BU", ordinary)
    assert winacl.user_trustees({"LA", "SY", "BA"}, admin) == {"LA"}
    assert winacl.user_trustees({ordinary, "SY"}, ordinary) == {ordinary}


_ME = "S-1-5-21-111-222-333-1001"


@pytest.mark.parametrize(
    ("sddl", "sid", "owner_only"),
    [
        (f"D:PAI(A;;FA;;;SY)(A;;FA;;;BA)(A;;0x12019f;;;{_ME})", _ME, True),
        (f"D:P(A;;0x12019f;;;{_ME})(A;;FR;;;IU)", _ME, False),
        (f"D:P(A;;0x12019f;;;{_ME})(A;;FR;;;BU)", _ME, False),
        (f"D:P(A;;0x12019f;;;{_ME})(A;;FR;;;S-1-5-21-111-222-333-1002)", _ME, False),
        (f"D:P(D;;FA;;;BU)(A;;0x12019f;;;{_ME})", _ME, True),
        ("D:P(A;;FA;;;SY)(A;;0x12019f;;;LA)", "S-1-5-21-111-222-333-500", True),
        ("D:P(A;;FA;;;SY)(A;;0x12019f;;;LA)", _ME, False),
        ("D:NO_ACCESS_CONTROL", _ME, False),
        (f'D:P(A;;0x12019f;;;{_ME})(XA;;FR;;;WD;(@User.Dept=="x"))', _ME, False),
    ],
    ids=[
        "owner-and-privileged",
        "interactive",
        "users",
        "another-account",
        "a-deny-narrows",
        "owner-as-LA",
        "LA-is-not-this-account",
        "null-dacl",
        "conditional-ace",
    ],
)
def test_the_read_back_names_a_file_owner_only_only_when_it_is(
    sddl: str, sid: str, owner_only: bool
) -> None:
    """The judgement ``restrict_to_owner`` passes on the DACL it left, on every platform: any
    grant beyond this account, SYSTEM, Administrators and the owner placeholders is a file
    other principals can read (review of #65, R3).

    Off Windows too, on purpose: ``LA`` is how a runner logged in as the built-in
    Administrator sees its own account, a shape no desktop produces, and misreading it as
    an intruder would make every sign-in on CI warn over a DACL that is correct."""
    assert paths._grants_only_owner(sddl, sid) is owner_only


@pytest.fixture
def windows_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[list[list[str]]]:
    """``restrict_to_owner``'s Windows branch on any platform: ``whoami`` and ``icacls``
    answered by a fake, the argv of every call recorded."""
    import subprocess

    ran: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        ran.append(list(argv))
        out = f'"host\\me","{_ME}"\r\n' if argv[0].endswith("whoami.exe") else ""
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("SYSTEMROOT", str(tmp_path / "Windows"))
    monkeypatch.setattr(subprocess, "run", fake_run)
    paths._whoami_sid.cache_clear()
    yield ran
    paths._whoami_sid.cache_clear()


def test_the_tools_run_from_system32_and_whoami_is_asked_once(
    windows_tools: list[list[str]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """By bare name, ``CreateProcess`` finds an ``icacls.exe`` or ``whoami.exe`` planted in the
    current directory before System32's, and a fake ``whoami`` could hand the grant to
    Everyone. The SID is asked once per process, not once per restriction (review of #65,
    R8)."""
    monkeypatch.setattr(paths, "_dacl_sddl", lambda _path: f"D:P(A;;0x12019f;;;{_ME})")
    secret = tmp_path / "credentials"
    assert paths.restrict_to_owner(secret) is True
    assert paths.restrict_to_owner(secret) is True
    system32 = tmp_path / "Windows" / "System32"
    assert [argv[0] for argv in windows_tools] == [
        str(system32 / "whoami.exe"),
        str(system32 / "icacls.exe"),
        str(system32 / "icacls.exe"),
    ]
    assert f"*{_ME}:(R,W)" in windows_tools[1]


def test_a_grant_the_call_left_behind_is_reported(
    windows_tools: list[list[str]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``icacls`` exits 0 having removed only the three principals it names. The DACL read
    back still grants INTERACTIVE, so the file is not owner-only and the answer is False;
    a DACL that cannot be read back is not vouched for either."""
    secret = tmp_path / "credentials"
    monkeypatch.setattr(paths, "_dacl_sddl", lambda _path: f"D:P(A;;0x12019f;;;{_ME})(A;;FR;;;IU)")
    assert paths.restrict_to_owner(secret) is False
    monkeypatch.setattr(paths, "_dacl_sddl", lambda _path: None)
    assert paths.restrict_to_owner(secret) is False


def test_a_whoami_that_failed_is_asked_again(
    windows_tools: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a SID is cached. A ``whoami`` that failed once, a timeout on a loaded machine,
    must not leave every later restriction in the process reporting False."""
    import subprocess

    answers = iter([1, 0])

    def flaky(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        windows_tools.append(list(argv))
        code = next(answers)
        return subprocess.CompletedProcess(argv, code, stdout=f'"host\\me","{_ME}"', stderr="")

    monkeypatch.setattr(subprocess, "run", flaky)
    assert paths._current_user_sid() is None
    assert paths._current_user_sid() == _ME
    assert paths._current_user_sid() == _ME
    assert len(windows_tools) == 2
