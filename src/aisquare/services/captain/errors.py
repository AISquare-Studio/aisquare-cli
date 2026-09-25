"""The one refusal type every captain module raises — owned here, so no seam depends on a stub.

``Refused`` is what the owner hears after ``refused:``. The Actions server turns
it into an MCP error result; the attention queue (T7's ``queue.py``) raises it
for an item it cannot act on. It lives in its own module because ``queue.py``
is replaced whole by its card, and ``actions`` must never name a class only a
stub defines (review of #217, gate 1, item 5). ``Failed`` is the other half: a
failure, not a rule, which the owner hears after ``error:``.
"""

from __future__ import annotations


class Refused(Exception):
    """A rule said no. The message is what the owner hears after ``refused: ``."""


class Failed(Exception):
    """Something failed that a retry may fix (a held lock, a busy store): said after ``error: ``."""
