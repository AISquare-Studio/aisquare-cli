"""``paths.restrict_to_owner`` -- the guard on the API key and the serve token."""

from __future__ import annotations

import stat
import sys
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

    In practice the file is created by this process under this process's own
    home, so a stray explicit ACE for a domain group or service account is not
    a shape we produce. If that ever stops being true, this test is the one
    that should start failing.
    """
    from tests import winacl

    secret = tmp_path / "credentials"
    secret.write_text("token", encoding="utf-8")

    winacl.grant_users_group(secret)
    winacl.grant_other_principal(secret)
    before = winacl.dacl_trustees(secret)
    assert winacl.USERS_TRUSTEE in before, "the named leak was not manufactured"
    assert winacl.OTHER_PRINCIPAL_TRUSTEE in before, "the unnamed grant was not manufactured"

    assert paths.restrict_to_owner(secret) is True

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
