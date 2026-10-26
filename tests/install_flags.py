"""The flags the install-script tests share, in a module that is not a test.

``tests/test_install_script_handoff.py`` skips itself at module level on Windows, so a value
imported from it is not defined there for mypy (``Cannot determine type of "DRY"``).
"""

from __future__ import annotations

#: The flags of a run that ends quickly and changes nothing.
DRY = ("--dry-run", "--yes", "--no-project", "--no-agent", "--no-system-deps")
