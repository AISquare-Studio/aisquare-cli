"""The program a person is told to run: the first word of every command a hint prints.

The product is rlx (the rlx rename), and ``rlx`` starts it, beside ``relax``, ``asq`` and
``aisquare``. So a hint says ``rlx accounts add``. But ``rlx`` is a short name, and another
program may hold it: PyPI's ``relax`` is another project, and a user's own ``rlx`` earlier
on PATH answers to the name before ours does. A hint that says ``rlx …`` then sends the user
to that program. So the word comes from this one place, :func:`word`, which says ``rlx``
only while the ``rlx`` a shell finds first is this install's own, and ``aisquare`` otherwise.

Every user-facing line that names a command takes its first word from here: a literal
``rlx …`` or ``aisquare …`` outside this module is what ``tests/test_program_name.py``'s
guard fails on, except where the line names what really runs (a hook's command line, the
agents' protocol). The product's name in prose ("rlx is quitting") is the plain word, as
``core.version.PRODUCT_NAME`` is: it names the product, not a command to type.
"""

from __future__ import annotations

import contextlib
import functools
import os
import shutil
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

from aisquare.core.shell_line import command_line

COMMAND = "rlx"
"""The word a hint's command starts with while it runs this install (:func:`word`)."""
FALLBACK = "aisquare"
"""The word it starts with otherwise: the name this install has always answered to."""
PROTOCOL = "aisquare"
"""The word the agents' protocol text names its commands with (the start pack, the role
cycles, the commands an agent is told to run): unchanged by the rename (#135, Q9), and the
same command either way."""
PRINTED = (COMMAND, FALLBACK)
"""Every word a printed command can start with. ``services.onboarding`` reads the doctor's
hints back into buttons, so it matches each of these."""


@functools.cache
def word() -> str:
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


IN_APP_ENV = "AISQUARE_IN_APP"
"""Set to ``1`` in the environment of the ``doctor --json`` asq runs to show its report: the
rows' details then leave out their commands (:func:`detail_advice`), while every fix stays the
shell's, which asq hands whole to the manager. Never in an agent's environment."""

_in_app = False
_here = threading.local()


def enter_app() -> None:
    """asq (or ``board -w``) is up in this process: what it shows a person never names a command
    to type (the owner, R13: "the whole purpose of TUI is to make it for users to never use the
    command line since their manager is aware of it"). Every shared message's way out that is
    a command, :func:`shell_advice`, goes from here until :func:`leave_app`."""
    global _in_app
    _in_app = True


def leave_app() -> None:
    """The app is down: what this process prints next is read at a shell (run_ui's hand-off)."""
    global _in_app
    _in_app = False


def in_app() -> bool:
    """Whether what this process says now is shown in asq: the app is up here
    (:func:`enter_app`), and this thread is not building for a shell (:func:`at_a_shell`)."""
    return _in_app and not getattr(_here, "shell", False)


@contextlib.contextmanager
def at_a_shell() -> Iterator[None]:
    """What this thread builds inside keeps its shell advice, though asq is up: the doctor's
    report, whose every fix asq hands whole to the manager (R13's "Ask the manager") and reads
    its buttons from. Its details' commands (:func:`detail_advice`) still go where asq shows
    them."""
    before = getattr(_here, "shell", False)
    _here.shell = True
    try:
        yield
    finally:
        _here.shell = before


def shell_advice(text: str, app: str = "") -> str:
    """``text``, a clause of a shared message that tells a person a command to type, for a
    shell; in asq, ``app`` instead: nothing by default, or the way in asq.

    The CLI's text is byte-for-byte what it was, and asq gets the sentence without its tail:
    nothing travels in the text to be cut out later. A message for an agent never goes
    through here: an agent acts on the commands, wherever it is shown."""
    return app if in_app() else text


def detail_advice(text: str, app: str = "") -> str:
    """:func:`shell_advice` for a clause of a doctor row's DETAIL, dropped wherever asq shows
    the report: built in asq's process, at a shell (``diagnostics.doctor`` builds every fix
    there) or not, and in the ``doctor --json`` asq runs to show it (:data:`IN_APP_ENV`). Never
    for a fix."""
    return app if _in_app or os.environ.get(IN_APP_ENV) == "1" else text


def words(*args: str) -> str:
    """``rlx <args>`` (or ``aisquare <args>``, :func:`word`) as one line a person can paste
    into their shell, quoted for it (``core.shell_line.command_line``)."""
    return command_line([word(), *args])


__all__ = [
    "COMMAND",
    "FALLBACK",
    "IN_APP_ENV",
    "PRINTED",
    "PROTOCOL",
    "at_a_shell",
    "detail_advice",
    "enter_app",
    "in_app",
    "leave_app",
    "ours_on_path",
    "shell_advice",
    "word",
    "words",
]
