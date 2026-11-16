"""The program a person is told to run: the first word of every command a hint prints.

The product is rlx (the rlx rename), and ``rlx`` starts it, beside ``relax``, ``asq`` and
``aisquare``. So a hint says ``rlx accounts add``. But ``rlx`` is a short name, and another
program may hold it: PyPI's ``relax`` is another project, and a user's own ``rlx`` earlier
on PATH answers to the name before ours does. A hint that says ``rlx …`` then sends the user
to that program. So the word comes from this one place, :func:`command`, which says ``rlx``
only while the ``rlx`` a shell finds first is this install's own, and ``aisquare`` otherwise.

Every user-facing line that names a command takes its first word from here: a literal
``rlx …`` or ``aisquare …`` outside this module is what ``tests/test_program_name.py``'s
guard fails on, except where the line names what really runs (a hook's command line, the
agents' protocol). The product's name in prose ("rlx is quitting") is the plain word, as
``core.version.PRODUCT_NAME`` is: it names the product, not a command to type.
"""

from __future__ import annotations

import functools
import os
import shutil
import sys
from pathlib import Path

from aisquare.core.shell_line import command_line

COMMAND = "rlx"
"""The word a hint's command starts with while it runs this install (:func:`command`)."""
FALLBACK = "aisquare"
"""The word it starts with otherwise: the name this install has always answered to."""
PRINTED = (COMMAND, FALLBACK)
"""Every word a printed command can start with. ``services.onboarding`` reads the doctor's
hints back into buttons, so it matches each of these."""


@functools.cache
def command() -> str:
    """``rlx`` when the ``rlx`` on PATH is this install's own (:func:`ours_on_path`), else
    ``aisquare``: another program's ``rlx`` comes first, or none is on PATH (a checkout run as
    ``python -m aisquare``, a PATH without ``~/.local/bin``).

    Once per process: the answer is this process's PATH and install, which a hint printed
    later in the same run shares.
    """
    return COMMAND if ours_on_path(COMMAND) else FALLBACK


def ours_on_path(name: str) -> bool:
    """Whether the ``name`` a shell finds first on PATH is this install's script of that name,
    the one beside its ``aisquare`` (``agents.current_install``). Compared by the file a link
    leads to, as uv's ``~/.local/bin`` entries are links, and case-folded where the platform
    folds case (Windows). ``False`` when none is on PATH or a path cannot be resolved."""
    found = shutil.which(name)
    if found is None:
        return False
    # Lazily: core.agents imports much, and a hint is printed far less often than this module
    # is imported.
    from aisquare.core.agents import current_install

    suffix = ".exe" if sys.platform == "win32" else ""
    mine = current_install().with_name(f"{name}{suffix}")
    try:
        return os.path.normcase(Path(found).resolve()) == os.path.normcase(mine.resolve())
    except (OSError, RuntimeError):  # a link loop, a directory this user cannot enter
        return False


def line(*args: str) -> str:
    """``rlx <args>`` (or ``aisquare <args>``, :func:`command`) as one line a person can paste
    into their shell, quoted for it (``core.shell_line.command_line``)."""
    return command_line([command(), *args])


__all__ = ["COMMAND", "FALLBACK", "PRINTED", "command", "line", "ours_on_path"]
