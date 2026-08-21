"""Reading Windows ACLs in tests, by SID rather than by display name.

``icacls`` prints names, and names are the wrong thing to assert on twice
over: they contain spaces (``OWNER RIGHTS``, ``NT AUTHORITY\\SYSTEM``) so they
do not parse on whitespace, and they are localized, so an English-only
assertion fails on a German runner over nothing. SIDs are stable, unique and
the same everywhere.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

PRIVILEGED_SIDS = frozenset(
    {
        "S-1-5-18",  # NT AUTHORITY\SYSTEM
        "S-1-5-32-544",  # BUILTIN\Administrators
        "S-1-3-4",  # OWNER RIGHTS
        "S-1-3-0",  # CREATOR OWNER
    }
)
"""The machine's own plumbing.

Their presence is not a leak: an administrator can take ownership of any file
regardless of its ACL, which is the same deal POSIX offers, where 0600 never
excluded root. What must never appear is Users, Everyone or Authenticated
Users -- an ordinary second account.
"""

USERS_SID = "S-1-5-32-545"
"""BUILTIN\\Users -- every interactive account on the box. A leak if granted."""


def powershell(script: str, **env: str) -> str:
    """Run a PowerShell snippet and return its stdout."""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=True,
        env={**os.environ, **env},
    )
    return result.stdout


def ace_sids(path: Path) -> set[str]:
    """The SIDs holding an ACE on ``path``.

    The path travels by environment variable rather than being interpolated
    into the script, so a quote or a ``$`` in a temp path cannot end up parsed
    as PowerShell.
    """
    out = powershell(
        "(Get-Acl -LiteralPath $env:ACL_TARGET).Access | ForEach-Object { "
        "$_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value }",
        ACL_TARGET=str(path),
    )
    return {line.strip() for line in out.splitlines() if line.strip()}


def current_user_sid() -> str:
    """The SID of the account running the tests."""
    return powershell(
        "[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value"
    ).strip()


def grant_users_group(path: Path) -> None:
    """Grant BUILTIN\\Users read access to ``path`` -- i.e. manufacture a leak.

    By SID (``*S-1-5-32-545``), because ``icacls`` accepts either and the name
    is localized.
    """
    subprocess.run(
        ["icacls", str(path), "/grant", f"*{USERS_SID}:(R)"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=True,
    )
