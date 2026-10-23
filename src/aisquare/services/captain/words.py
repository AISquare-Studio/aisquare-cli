"""How the owner answers the captain's own confirmation question (T1d).

The words live here, not in ``actions``, so :func:`owner_said` can run on every owner line
``brain.say`` delivers without loading the Actions server.
"""

from __future__ import annotations

import re
from collections.abc import Collection

from aisquare.services.captain import state as captain_state

WORDS = re.compile(r"[a-z0-9]+")

AFFIRMATIVES = ("yes", "yeah", "yep", "do it", "go ahead", "confirm", "confirmed")
"""How the owner says yes to the captain's question: the utterance begins with one (13570)."""

WHOLE_AFFIRMATIVES = ("ok", "okay", "sure", "yup", "roger", "copy", "affirmative")
"""Yes only as the WHOLE utterance, punctuation aside (13614): "OK, what's up" is no yes."""

NO_WORDS = ("no", "nope", "nah", "never", "negative", "don't", "dont", "do not")
"""How a refusal BEGINS (review of #240, finding 1): "No, leave coder-1 running" is a no,
though it names coder-1. This overturns round 4's "a no only as the whole utterance":
"No problem, go ahead" is a no now, and costs the owner one more sentence."""

NEGATIVES = (*NO_WORDS, "cancel", "stop that")
"""The owner's no as the WHOLE utterance (T1d round 4): it refuses and clears the live
question. "cancel" and "stop that" are a no only alone: "Stop that coder in alpha" is a
named stop."""

NEGATIONS = (
    "don't",
    "dont",
    "do not",
    "not",
    "never",
    "shouldn't",
    "shouldnt",
    "mustn't",
    "mustnt",
)
"""What refuses an action from right before its verb, anywhere in the words: "Please don't
stop coder-1", "I would not restart coder-1", "You shouldn't stop coder-1". Not "won't" or
"can't": "coder-1 won't stop, force-stop it" is an order."""

ACTION_VERBS: dict[str, tuple[str, ...]] = {
    "stop": ("stop", "force stop"),
    "restart": ("restart",),
    "spawn": ("spawn",),
}
"""The verb each confirming tool stands for, as its own question says it ("Stop coder-1 in
alpha?", "Force-stop …?"). No synonyms: neither the gate nor the persona reads another word
as one of these, and "it did not start" is why an owner restarts an agent."""

KEEPS = ("leave", "keep")
"""What refuses an action from right before the target's name: "Leave coder-1 alone",
"keep the coder running"."""

KEEP_REACH = 2
"""How many spoken words after :data:`KEEPS` the target's name may begin. "Stop coder-1 and
keep its worktree" is an order."""


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


def _negated(utterance: str, verbs: tuple[str, ...]) -> bool:
    """One of :data:`NEGATIONS` stands right before one of ``verbs``."""
    said = WORDS.findall(utterance.lower())
    refused = [WORDS.findall(f"{negation} {verb}") for negation in NEGATIONS for verb in verbs]
    return any(
        said[index : index + len(phrase)] == phrase
        for phrase in refused
        for index in range(len(said))
    )


def _kept(utterance: str, named_at: Collection[int]) -> bool:
    """One of :data:`KEEPS` stands at most :data:`KEEP_REACH` spoken words before a place the
    target is named. Spoken words are what a space parts: "Dave's" and "coder-1" are one
    each, where :data:`WORDS` reads two."""
    heard = [
        (word, spoken)
        for spoken, chunk in enumerate(utterance.lower().split())
        for word in WORDS.findall(chunk)
    ]
    return any(
        word in KEEPS and index < at and heard[at][1] - spoken <= KEEP_REACH
        for index, (word, spoken) in enumerate(heard)
        for at in named_at
    )


def refusal(utterance: str, *, tool: str, named_at: Collection[int] = ()) -> bool:
    """Whether the owner's words refuse ``tool``'s action, even when they name its target.

    The persona passes every owner answer with confirm=true, a no included, so the server
    is the gate, and a refusal names the agent as readily as an order does: "No, leave
    coder-1 running" stopped coder-1 (review of #240, finding 1). The words are a refusal
    when, case and punctuation aside and as whole words:

    1. they are a no (:data:`NEGATIVES`), or BEGIN with one (:data:`NO_WORDS`);
    2. a negation (:data:`NEGATIONS`) stands right before the tool's own verb
       (:data:`ACTION_VERBS`), anywhere in them;
    3. leave or keep (:data:`KEEPS`) stands at most :data:`KEEP_REACH` words before a name
       of the target. ``named_at`` is where the gate reads the target named, as indexes
       into the utterance's :data:`WORDS`: names are the gate's to match, on its board.

    Why this list and no more: the owner's choice (2026-09-30) of CLEAR refusals over the
    review's "a negation anywhere". "Stop coder-1, no need for it anymore" and "Restart
    coder-1, it is not responding" are orders, and reading every no or not as a refusal
    would have the captain ask twice for them. The price, taken knowingly: a refusal in
    other words ("coder-1 should carry on") is not read here. What is read errs toward the
    no, because the gate fails toward asking again: words wrongly taken for a refusal do
    nothing and cost the owner one more sentence; words wrongly taken for an order stop an
    agent.
    """
    return (
        negative(utterance)
        or _starts(utterance, NO_WORDS)
        or _negated(utterance, ACTION_VERBS[tool])
        or _kept(utterance, named_at)
    )


def owner_said(line: str) -> None:
    """Every owner line the captain is given (``captain say``, chat, the voice page) clears a
    pending confirmation question unless the line is itself a yes (T1d round 5, 14404 (c)):
    a no the captain answers on its own, or any other line, never leaves a stop armed."""
    if not affirmative(line):
        captain_state.clear_pending()
