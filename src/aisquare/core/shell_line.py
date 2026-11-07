"""One command line a person pastes into their shell, and that line read back.

The writer every pasteable command goes through (``services.install_route``'s
remove and fix lines, an adapter's removal command for what ``aisquare uninstall`` leaves:
``AgentAdapter.lasting_installs``): POSIX quoting, and on Windows the quoting cmd.exe and
PowerShell both read as written. Pure; no I/O.
"""

from __future__ import annotations

import re
import shlex
import sys
from collections.abc import Sequence

#: An argument cmd.exe and PowerShell both pass on as it is, unquoted. Anything else — a
#: space, cmd's ``< > | & ^``, PowerShell's ``, ; ( ) { }`` or a leading ``@`` — is
#: double-quoted, which both read literally, unless it holds :data:`_EXPANDED`.
_WINDOWS_BARE = re.compile(r"(?!@)[\w@+=:./\\\[\]-]+", re.ASCII)
_EXPANDED = re.compile("[$`%]")
"""What a shell expands inside double quotes: PowerShell's ``$name`` and backtick escapes,
cmd.exe's ``%NAME%``. No quoting both shells share protects them, so such a word is quoted
for PowerShell, Windows' own shell (Windows Terminal opens it, ``install.ps1`` runs in it):
in single quotes, which it reads literally (review of #259, round 21). cmd.exe passes those
quotes on as part of the word, so there the command names no such file and fails, rather
than acting on the directory an expansion made of it."""
_POWERSHELL_QUOTES = "'\u2018\u2019\u201a\u201b"
"""What PowerShell reads as a single quote: the ASCII one and the typographic ones."""


def command_line(argv: Sequence[str]) -> str:
    """``argv`` as one line a person can paste into their shell.

    On Windows that shell is cmd.exe or PowerShell. ``subprocess.list2cmdline`` quotes
    only for the C runtime, so ``--with tiktoken>=0.7`` came out bare, and cmd read
    ``>=0.7`` as a redirection: the constraint was dropped (sweep of #257). A word either
    shell would expand is quoted for PowerShell (:data:`_EXPANDED`).
    """
    if sys.platform == "win32":
        return " ".join(_windows_word(arg) for arg in argv)
    return shlex.join(argv)


def _windows_word(arg: str) -> str:
    """``arg`` as :func:`command_line` writes it on Windows: bare, double-quoted, or for a word
    a shell would expand (:data:`_EXPANDED`), PowerShell's single quotes, each single quote
    in it doubled."""
    if _EXPANDED.search(arg):
        return "'" + re.sub(f"([{_POWERSHELL_QUOTES}])", r"\1\1", arg) + "'"
    return arg if _WINDOWS_BARE.fullmatch(arg) else _windows_quoted(arg)


def split_line(text: str) -> list[str]:
    """The arguments of one :func:`command_line`, read back as the shell it was written for
    reads them, so a fix printed for pasting is also the command a button runs. Raises
    ``ValueError`` for a quote it does not close."""
    if sys.platform != "win32":
        return shlex.split(text)
    quotes = _POWERSHELL_QUOTES
    words = [
        found.group()
        for found in re.finditer(
            rf'"(?:[^"\\]|\\.)*"|\'(?:[^{quotes}]|([{quotes}])\1)*\'|[^\s"\']+|["\']', text
        )
    ]
    if '"' in words or "'" in words:
        raise ValueError(f"no closing quotation in {text!r}")
    return [_windows_unquoted(word) for word in words]


def _windows_unquoted(token: str) -> str:
    """``token`` as :func:`_windows_quoted` was given it: its outer quotes taken off, and the
    backslashes it doubled before a quote or at the end halved. A single-quoted one, as
    :func:`_windows_word` was: its quotes taken off and each doubled quote in it halved."""
    if len(token) >= 2 and token[0] == token[-1] == "'":
        return re.sub(f"([{_POWERSHELL_QUOTES}])\\1", r"\1", token[1:-1])
    if len(token) < 2 or token[0] != '"' or token[-1] != '"':
        return token
    inner = re.sub(r'(\\*)\\"', lambda m: m.group(1)[: len(m.group(1)) // 2] + '"', token[1:-1])
    return re.sub(r"(\\+)\Z", lambda m: m.group(1)[: len(m.group(1)) // 2], inner)


def _windows_quoted(arg: str) -> str:
    """``arg`` in double quotes, escaped by the C runtime's rules (``list2cmdline``'s): its
    own quotes, and the backslashes before them or before the closing quote, doubled."""
    escaped = re.sub(r'(\\*)"', r'\1\1\\"', arg)
    return '"' + re.sub(r"(\\+)\Z", r"\1\1", escaped) + '"'
