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
    assert winacl.USERS_SID in winacl.ace_sids(secret), "the leak was not manufactured"

    assert paths.restrict_to_owner(secret) is True

    granted = winacl.ace_sids(secret)
    me = winacl.current_user_sid()
    assert winacl.USERS_SID not in granted, granted
    assert me in granted, granted
    assert not (granted - winacl.PRIVILEGED_SIDS - {me}), granted
