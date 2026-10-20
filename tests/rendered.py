"""Rendered CLI output flattened for content asserts: no ANSI, no wrapping.

Not a test module. Four test files carried their own copy of this pair, and a
fifth asserted on the raw output instead — green locally, red on GitHub
Actions (review of #203): typer forces a terminal whenever ``GITHUB_ACTIONS``
is set, so its error panel arrives styled and wrapped to the panel's width.
"""

from __future__ import annotations

import re

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
"""A CSI escape sequence: colour, bold and dim alike."""


def plain(text: str) -> str:
    """``text`` without escape codes, its whitespace runs collapsed to one space.

    ``NO_COLOR`` alone is not enough — rich keeps non-color attributes
    (bold/dim) under it, and typer's highlighter styles the leading ``-`` of
    an option as its own span, so escape codes land INSIDE tokens like
    ``--json`` on forced-color environments (GitHub Actions). The collapse
    undoes the panel's wrapping, which splits a sentence at the panel's width.
    """
    return " ".join(ANSI.sub("", text).split())
