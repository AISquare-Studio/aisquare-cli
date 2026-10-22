"""The bundled personas a test expects, with the experimental captain off and on.

Both catalogues are written out here, never read from the code under test. Off, as it
ships, it is #201's four. On, the captain RC's own ``captain`` persona is listed too:
``core.personas`` hides it while the switch is off. The suite runs both ways
(``AISQUARE_SUITE_CAPTAIN`` in ``tests/conftest.py``), and the switch is set per test, so
a test asks here at run time, never at import (review of #240, F9).
"""

from __future__ import annotations

from aisquare.core import experimental

OFF = ("careful", "mentor", "minimalist", "skeptic")
"""#201's catalogue: what ships, with the captain off."""

ON = ("captain", "careful", "mentor", "minimalist", "skeptic")
"""With the captain on: #201's four, and the captain's own persona first."""


def names() -> list[str]:
    """This run's bundled catalogue, in the order every listing gives it."""
    return list(ON if experimental.captain_enabled() else OFF)


def known() -> str:
    """The tail of an unknown-persona refusal: ``known: careful, mentor, …``."""
    return "known: " + ", ".join(names())
