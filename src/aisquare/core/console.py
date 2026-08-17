"""Rich console factories that honour the global output flags.

These two functions are the ONLY place a ``Console`` is built, which is what
makes ``markup=False`` below a property of the CLI rather than of ninety call
sites. ``tests/test_console_markup.py`` walks the package AST and fails if a
``Console`` is constructed anywhere else.

THE INVENTORY the markup sweep asked for, and how to re-derive it: an AST scan
for ``.print`` / ``.add_row`` / ``.add_column`` calls whose arguments carry an
f-string, a ``.format`` or a bare name found **87 sites that interpolate
external data** — paths, git refs, role names, config values, binary names,
URLs, and remembered context text. All 87 are safe by construction here rather
than one at a time. Two groups needed direct handling: the six sites in
``cli/launch`` that deliberately styled text now carry that styling as
``style=`` or a ``rich.text.Text``, neither of which reaches the parser; and
``emit_doctor`` stopped calling ``rich.markup.escape``, which would otherwise
print the backslash it exists to hide now that nothing parses markup.
"""

from __future__ import annotations

import contextlib
import io
import sys

from rich.console import Console

from aisquare.core.state import get_state


def _ensure_utf8(stream: object) -> None:
    """Make a redirected Windows stream able to carry non-ASCII output.

    Attached to a console, Windows streams are already UTF-8. Redirected to a
    file or a pipe they fall back to the ANSI codepage (cp1252 in most
    installs), which cannot encode the ``✓``/``⚠``/``→`` this CLI prints — so
    ``aisquare doctor > out.txt`` dies with ``UnicodeEncodeError`` while the
    same command run interactively is fine. Reconfiguring the stream keeps the
    intended symbols rather than degrading them to ASCII.
    """
    if sys.platform != "win32" or not isinstance(stream, io.TextIOWrapper):
        return
    if (stream.encoding or "").lower().replace("-", "") == "utf8":
        return
    # A detached or already-closed stream cannot be reconfigured; printing is
    # about to fail anyway, and this must not be what raises.
    with contextlib.suppress(OSError, ValueError):
        stream.reconfigure(encoding="utf-8", errors="replace")


#: ``markup=False`` is the load-bearing default, not a preference. Rich reads
#: ``[...]`` as a style tag and DELETES it, and almost everything this CLI
#: prints interpolates data it does not control — paths, git refs, role names,
#: config values, binary names, URLs, remembered text. Two independent lanes
#: shipped the same silent bug in one night: the install hint reached users as
#: ``pip install 'aisquare-cli'`` with the extra name gone, and the doctor's
#: detail column ate ``[present]`` so a configured key read as a missing one.
#: Neither raised; both printed a confident wrong answer.
#:
#: Set HERE rather than at ~90 call sites because the default is what the next
#: call site inherits. It covers Rich tables too — cells are parsed the same
#: way, which is how ``context list`` was mangling remembered entries.
#:
#: Deliberate styling is unaffected: ``style=`` arguments, ``Column(style=…)``,
#: table ``header_style`` and ``rich.text.Text`` all bypass the markup parser.
#: Anything that genuinely needs inline tags passes ``markup=True`` and escapes
#: its own data — there is exactly one such place, and it uses ``Text`` instead.
_MARKUP = False


def stdout_console() -> Console:
    """Console for regular program output."""
    _ensure_utf8(sys.stdout)
    return Console(no_color=get_state().no_color, highlight=False, soft_wrap=True, markup=_MARKUP)


def stderr_console() -> Console:
    """Console for diagnostics and errors."""
    _ensure_utf8(sys.stderr)
    return Console(
        stderr=True,
        no_color=get_state().no_color,
        highlight=False,
        soft_wrap=True,
        markup=_MARKUP,
    )
