"""How the owner answers the captain's own confirmation question (T1d).

The words live here, not in ``actions``, so :func:`owner_said` can run on every owner line
``brain.say`` delivers without loading the Actions server.
"""

from __future__ import annotations

import re

from aisquare.services.captain import state as captain_state

WORDS = re.compile(r"[a-z0-9]+")

AFFIRMATIVES = ("yes", "yeah", "yep", "do it", "go ahead", "confirm", "confirmed")
"""How the owner says yes to the captain's question: the utterance begins with one (13570)."""

WHOLE_AFFIRMATIVES = ("ok", "okay", "sure", "yup", "roger", "copy", "affirmative")
"""Yes only as the WHOLE utterance, punctuation aside (13614): "OK, what's up" is no yes."""

NEGATIVES = ("no", "nope", "cancel", "negative", "don't", "stop that")
"""The owner's no, only as the WHOLE utterance like the whole-utterance yeses (T1d round 4):
it refuses and clears the live question. "No problem, go ahead" is neither yes nor no, and
"Stop that coder in alpha" is a named stop."""


def _starts(utterance: str, phrases: tuple[str, ...]) -> bool:
    said = WORDS.findall(utterance.lower())
    return any(said[: len(w)] == w for w in map(WORDS.findall, phrases))


def _whole(utterance: str, phrases: tuple[str, ...]) -> bool:
    said = WORDS.findall(utterance.lower())
    return any(said == w for w in map(WORDS.findall, phrases))


def affirmative(utterance: str) -> bool:
    """A yes: begins with one of :data:`AFFIRMATIVES`, or is one of
    :data:`WHOLE_AFFIRMATIVES` whole."""
    return _starts(utterance, AFFIRMATIVES) or _whole(utterance, WHOLE_AFFIRMATIVES)


def negative(utterance: str) -> bool:
    """A no: one of :data:`NEGATIVES`, as the whole utterance."""
    return _whole(utterance, NEGATIVES)


def owner_said(line: str) -> None:
    """Every owner line the captain is given (``captain say``, chat, the voice page) clears a
    pending confirmation question unless the line is itself a yes (T1d round 5, 14404 (c)):
    a no the captain answers on its own, or any other line, never leaves a stop armed."""
    if not affirmative(line):
        captain_state.clear_pending()
