"""Experimental features: each one switch, read here and nowhere else.

``captain`` — the home-level agent (``docs/captain.md``) ships OFF. The owner,
2026-09-29: "feature flag off captain for now, allow experimentally" (card
tsk_01m3qvghrpgg). ``[experimental] captain = true`` in config.toml turns it on
(``aisquare config set experimental.captain true``), and
``AISQUARE_EXPERIMENTAL_CAPTAIN`` wins over the config in both directions, as
``AISQUARE_CI`` does over ``[experiment]``: an on-value turns it on, anything else
set turns it off (``0``, and a typo), and unset or empty defers to the config.

Off: ``aisquare captain`` and every subcommand exit 2 with :data:`CAPTAIN_OFF`, the
fleet UI shows no insignia, captain row or captain view and its ui receiver refuses
the captain's actions, the bundled ``captain`` persona is absent, the voice page does
not serve (it is a subcommand), and doctor says so in one ok row. On, all of it is as
it was before the switch. Nothing else reads it: fleets, boards and the store are the
same either way.
"""

from __future__ import annotations

import os

from aisquare.core.config import load_config

CAPTAIN_ENV = "AISQUARE_EXPERIMENTAL_CAPTAIN"

CAPTAIN_OFF = (
    "the captain is experimental; turn it on with aisquare config set experimental.captain "
    f"true, or {CAPTAIN_ENV}=1"
)
"""The one line every refusal says: the CLI's, the ui receiver's and doctor's."""

_ON_VALUES = frozenset({"1", "true", "yes", "on"})


def captain_enabled() -> bool:
    """Whether the captain is on: the variable when set, else ``[experimental] captain``.

    Read at every call, never cached: a command asks once, the fleet UI once at its
    start, and a persona walk only on the bundled ``captain`` directory. A config that
    cannot be read opts into nothing; doctor and every command that loads it say why.
    """
    override = os.environ.get(CAPTAIN_ENV, "").strip().lower()
    if override:
        return override in _ON_VALUES
    try:
        return load_config().experimental.captain
    except Exception:  # an unreadable config is no opt-in
        return False
