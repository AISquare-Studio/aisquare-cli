"""Scrub credentials out of text on its way off the machine.

``config.redaction.level`` has existed since the first release and, until this
module, nothing read it: setting ``strict`` changed no behaviour anywhere, which
is worse than having no setting at all — an operator who set it believed they
were protected. This is the first consumer, and it makes the existing knob true.

WHAT THIS IS FOR. #50 ships human prompts and board events to the gateway.
Prompts are typed by people, and people paste keys into terminals — usually
while asking why the key does not work. The paste is the incident; everything
here exists for the seconds after it.

WHERE IT RUNS. On the shipping path only (``core.insights``), never on local
capture. ``aisquare log`` and the board row keep exactly what was typed: that
text never left the machine, and quietly rewriting a user's own history is a
different decision that nobody asked for and that would make the local record
useless for the debugging it exists to support.

THE LEVELS, and why they split where they do:

``off``     ship as typed. Someone who sets this has decided; we do not
            second-guess them.
``standard`` (default) credentials only. A pasted key is an incident; a file
            path is the substance of an engineering prompt. Redacting paths and
            hostnames by default would gut the dataset the whole night exists to
            produce, in exchange for a risk nobody has articulated.
``strict``  credentials, plus who and where — email addresses and home
            directories. For a workspace whose policy says the gateway may not
            learn who typed something.

FALSE POSITIVES ARE A REAL COST here, not a nuisance: every over-match is a
sentence the collective-intelligence dataset cannot learn from. The patterns are
therefore keyed to credential SHAPES that do not occur in prose, and
``test_ordinary_prose_is_returned_unchanged`` is the guard against creep.

Every pattern is linear-time by construction — bounded character classes, no
nested quantifiers — because this runs on the primary path and a hook that hangs
is a session that hangs.
"""

from __future__ import annotations

import re

from aisquare.models import RedactionLevel

#: What replaces a redacted value. Visible on purpose: a silent scrub is
#: indistinguishable from a user who typed nothing, and a reader of the Run
#: needs to know a value WAS here before deciding the span is uninteresting.
MARKER = "[redacted]"

#: Credential shapes, matched on shape rather than on context. Order matters
#: only in that the structured forms (assignment, header, URL) run first so
#: their key name survives into the output.
_CREDENTIAL_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    # PEM blocks first: they contain base64 that later rules would shred into
    # a dozen markers, losing the fact that it was ONE key.
    (
        re.compile(
            r"-----BEGIN [A-Z ]{0,32}PRIVATE KEY-----.*?-----END [A-Z ]{0,32}PRIVATE KEY-----",
            re.DOTALL,
        ),
        MARKER,
    ),
    # NAME=value / NAME: value where the name says "secret". The name is kept —
    # "EXPLAINABILITY_API_KEY=[redacted]" still tells a reader a key was in play,
    # which a bare marker does not.
    (
        re.compile(
            r"(?i)\b([A-Z0-9_.-]{0,48}?(?:api[_-]?key|secret|token|password|passwd|pwd"
            r"|credential|private[_-]?key|access[_-]?key)[A-Z0-9_.-]{0,24})"
            r"(\s*[:=]\s*)"
            r"(\"[^\"\n]{4,200}\"|'[^'\n]{4,200}'|[^\s\"'\n]{4,200})"
        ),
        r"\1\2" + MARKER,
    ),
    # Authorization: Bearer <token> / Authorization: Basic <blob>
    (
        re.compile(r"(?i)\b(authorization\s*:\s*|bearer\s+|basic\s+)([A-Za-z0-9_.+/=~-]{12,400})"),
        r"\1" + MARKER,
    ),
    # user:password@host in a URL — the password is the point, but the whole
    # pair identifies the account, so both go.
    (re.compile(r"(?<=://)[^/\s:@]{1,128}:[^/\s:@]{1,256}@"), MARKER + "@"),
    # JWT: three base64url segments. Distinctive enough to match on shape.
    (
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,600}\.[A-Za-z0-9_-]{8,2000}\.[A-Za-z0-9_-]{8,600}\b"),
        MARKER,
    ),
    # Vendor-prefixed tokens. Prefixes chosen because they are issued, not
    # written: none of them occur in English or in code identifiers.
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,256}\b"), MARKER),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,256}\b"), MARKER),
    (re.compile(r"\bglpat-[A-Za-z0-9_-]{16,256}\b"), MARKER),
    (re.compile(r"\bxox[baprse]-[A-Za-z0-9-]{10,256}\b"), MARKER),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), MARKER),
    # Our own sign-in tokens (services/iam.py). Issued, never typed by hand.
    (re.compile(r"\baisqr?_[A-Za-z0-9_-]{16,256}\b"), MARKER),
    # A RANGE, not the documented exact length. The published Google key shape
    # is AIza + 35, and a pattern pinned to 35 misses a key that is 36 long —
    # which is exactly how a scrubber passes its tests and leaks in the field.
    # The prefix is what makes this specific; the length only bounds the match.
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,128}\b"), MARKER),
    (re.compile(r"\bwk_(?:live|test)_[A-Za-z0-9]{8,256}\b"), MARKER),
)

#: ``strict`` only. Who typed it and where they were — identity, not secrets.
_IDENTITY_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,255}\.[A-Za-z]{2,24}\b"), MARKER),
    # The home directory names the human; the path below it names the work, and
    # the work is the part worth keeping.
    (re.compile(r"(?:/home|/Users)/[A-Za-z0-9._-]{1,64}"), "~"),
    (re.compile(r"(?i)\bC:\\Users\\[A-Za-z0-9._ -]{1,64}"), "~"),
)


def redact(text: str, level: RedactionLevel) -> str:
    """Return ``text`` with credentials (and, at ``strict``, identity) removed.

    Never raises and never returns ``None``: this sits between a user pressing
    enter and their prompt being recorded, so the worst case must be "shipped
    unscrubbed" only if that is what the level says — a scrubber that throws
    would cost the record itself.
    """
    if level is RedactionLevel.off:
        return text
    try:
        scrubbed = text
        for pattern, replacement in _CREDENTIAL_RULES:
            scrubbed = pattern.sub(replacement, scrubbed)
        if level is RedactionLevel.strict:
            for pattern, replacement in _IDENTITY_RULES:
                scrubbed = pattern.sub(replacement, scrubbed)
        return scrubbed
    except Exception:  # a scrubber that throws costs the record it was protecting
        # Falling back to the RAW text would ship the very thing this exists to
        # stop, so the fallback is the safe direction: drop the body, keep the
        # fact that something was here.
        return MARKER
