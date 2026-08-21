"""Reading Windows ACLs in tests, by SID, using only ``icacls`` and ``whoami``.

Two dead ends are worth recording, because both looked correct locally:

* ``icacls <path>`` prints *display names*, which is wrong twice over -- they
  contain spaces (``OWNER RIGHTS``, ``NT AUTHORITY\\SYSTEM``) so they do not
  parse on whitespace, and they are localized, so an English-only assertion
  fails on a non-English machine over nothing.
* PowerShell ``Get-Acl`` returns structured objects and is the obvious answer,
  but ``Microsoft.PowerShell.Security`` does not autoload on the GitHub
  ``windows-latest`` runner (``CouldNotAutoloadMatchingModule``), so the
  cmdlet simply does not exist there. It works on a normal desktop, which is
  exactly what makes it a trap.

``icacls /save`` emits SDDL -- SIDs and well-known abbreviations, no name
resolution, no PowerShell, and the same binary the product itself uses.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

# SDDL trustee abbreviations (and their SIDs, for hosts that spell them out).
# Their presence on the DACL is not a leak: an administrator can take ownership
# of any file regardless, which is the same deal POSIX offers, where 0600 never
# excluded root.
PRIVILEGED_TRUSTEES = frozenset(
    {
        "SY",
        "S-1-5-18",  # NT AUTHORITY\SYSTEM
        "BA",
        "S-1-5-32-544",  # BUILTIN\Administrators
        "OW",
        "S-1-3-4",  # OWNER RIGHTS
        "CO",
        "S-1-3-0",  # CREATOR OWNER
    }
)

USERS_TRUSTEE = "BU"
"""BUILTIN\\Users -- every interactive account on the box. A leak if granted."""

USERS_SID = "S-1-5-32-545"
"""The same principal, spelled for ``icacls /grant`` (which accepts ``*<sid>``)."""

_ACE = re.compile(r"\(([^)]*)\)")


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"{argv[0]} exited {result.returncode}\n"
            f"--- argv ---\n{argv}\n"
            f"--- stdout ---\n{result.stdout}\n"
            f"--- stderr ---\n{result.stderr}"
        )
    return result


def current_user_sid() -> str:
    """The SID of the account running the tests, straight from ``whoami``."""
    out = _run(["whoami", "/user", "/fo", "csv", "/nh"]).stdout.strip()
    # '"domain\\user","S-1-5-21-..."'
    return out.split(",")[-1].strip().strip('"')


def dacl_trustees(path: Path) -> set[str]:
    """Every trustee on ``path``'s DACL, as an SDDL abbreviation or a SID.

    ``icacls /save`` only walks directories, so the parent is saved with ``/T``
    and the line belonging to ``path`` picked out. The output file is written
    outside that tree so it cannot enumerate itself.
    """
    with tempfile.TemporaryDirectory() as scratch:
        saved = Path(scratch) / "acl.txt"
        _run(["icacls", str(path.parent), "/save", str(saved), "/T", "/C"])
        # icacls writes UTF-16, and emits "<relative path>" then "D:<acl>" pairs.
        text = saved.read_bytes().decode("utf-16", errors="replace")

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for index, line in enumerate(lines[:-1]):
        if Path(line).name != path.name:
            continue
        dacl = lines[index + 1]
        if not dacl.startswith("D:"):
            continue
        # Each ACE is (type;flags;rights;object;inherit;TRUSTEE).
        return {fields[5] for ace in _ACE.findall(dacl) if len(fields := ace.split(";")) >= 6}
    raise AssertionError(f"no DACL found for {path.name} in:\n{text}")


def grant_users_group(path: Path) -> None:
    """Grant BUILTIN\\Users read access to ``path`` -- i.e. manufacture a leak.

    By SID (``*S-1-5-32-545``), because ``icacls`` accepts either form and the
    display name is localized.
    """
    _run(["icacls", str(path), "/grant", f"*{USERS_SID}:(R)"])
