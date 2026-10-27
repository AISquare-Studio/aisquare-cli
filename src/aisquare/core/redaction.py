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

Every pattern is linear-time by construction — possessive runs, no nested quantifiers,
and a start only where its token could start (see :data:`_CREDENTIAL_RULES`) — because this
runs on the primary path and a hook that hangs is a session that hangs.
"""

from __future__ import annotations

import re
from bisect import bisect_left

from aisquare.models import RedactionLevel

#: What replaces a redacted value. Visible on purpose: a silent scrub is
#: indistinguishable from a user who typed nothing, and a reader of the Run
#: needs to know a value WAS here before deciding the span is uninteresting.
MARKER = "[redacted]"

#: Where a token may start: after anything but an ASCII letter, digit or underscore. NOT
#: ``\b``, which is Unicode-aware and so needs a non-word character before a key and finds
#: none after text in another script (``我的密钥sk-ant-…``, ``clésk-…``: ``é`` and ``钥`` are
#: word characters), leaving the whole key in the text it was written to remove it from.
_START = r"(?<![A-Za-z0-9_])"

#: Where a secret's name may start: where a token may, and at a letter, digit or underscore. The
#: name's own first character is never a ``.`` or a ``-`` (the keyword it holds starts with a
#: letter, and a ``--`` or a ``.`` before it stays in the output either way), and a start at
#: every character of a long run of them cost each one the whole 48-character look ahead for a
#: keyword: 65 times what prose costs, per character, in a 500 KB run of ``-``.
_NAME_START = _START + "(?=[A-Za-z0-9_])"

#: What names a secret in an assignment: a keyword, with the rest of the name (up to 48 characters
#: before it and 24 after) kept in the output.
_SECRET_NAME = (
    r"[A-Z0-9_.-]{0,48}?(?:api[_-]?key|secret|token|password|passwd|pwd"
    r"|credential|private[_-]?key|access[_-]?key)[A-Z0-9_.-]{0,24}"
)

#: Where a quoted value may end: before a separator, a closing bracket or the end of the text. A
#: value is taken to the quote that closes it, across the escaped quotes inside it, only when
#: that quote is followed by one of these. Otherwise (a value that ends in a backslash, as
#: ``"C:\x\" k="v"`` does) it is taken to the first quote, as it was, and the text after it
#: goes to its own rules instead of being read into this value.
_VALUE_END = r"(?=[\s,;}\])]|\Z)"

#: A quoted value, to the quote that closes it. ``"ab\"cd"`` is one value that holds a quote (in
#: JSON, in Python, in a shell), and the first quote is not its end: a pattern that stopped there
#: shipped the rest of the value (``cd1234"}``), or all of it when fewer than four characters came
#: before the quote. Each form reads the escapes first and the first quote after, which is what
#: the rule did, for what is not well formed. Where the text is JSON inside a string its quotes
#: are escaped (``\"…\"``) and the quote of a value that holds one is escaped again (three
#: backslashes and a quote): that form closes at a quote with ONE backslash before it, which
#: needs no look at what follows, and reads the runs of backslashes before a quote as an escaped
#: quote (three), an escaped backslash (four) or a pair and a letter (``\\n``). The body of every
#: form stops at the first quote it does not know as an escape, so no value reads the text of
#: the next one: the cost stays linear.
_QUOTED_VALUE = (
    r'\\"(?:[^"\\\n]|\\\\\\"|\\\\\\\\|\\\\[^"\\\n]){4,}+\\"'
    + r'|\\++"[^"\n]{4,}+"'
    + r'|"(?:[^"\\\n]|\\[^\n]){4,}+"'
    + _VALUE_END
    + r'|"[^"\n]{4,}+"'
    + r"|'(?:[^'\\\n]|\\[^\n]){4,}+'"
    + _VALUE_END
    + r"|'[^'\n]{4,}+'"
)

#: Credential shapes, matched on shape rather than on context. Order matters
#: only in that the structured forms (assignment, header, URL) run first so
#: their key name survives into the output.
#:
#: NO LENGTH IS A CAP HERE. A credential is removed whole, however long: a rule that
#: stopped at 200, 256 or 400 characters left the rest of a 283-character JWT, a
#: 1,000-character bearer token and a 300-character key in the text it was written to
#: remove them from (tester on #15), and Google's and Azure's access tokens run 1,000 to
#: 1,500 characters. A minimum is a shape (a prefix and 16 more characters); a maximum is
#: a leak. What keeps the cost linear instead is the match itself: every run of a token's
#: characters is a POSSESSIVE quantifier (``{n,}+``: it takes the whole run and never gives
#: a character back), so an attempt that fails costs the run it read and no more, and a
#: rule starts only where its token could start (a prefix after :data:`_START`, a JWT at the
#: start of a run of its alphabet), so two attempts never read the same run. The PEM rule
#: is not a regular expression for this reason: see :func:`_without_pem_blocks`.
_CREDENTIAL_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    # NAME=value / NAME: value where the name says "secret". The name is kept —
    # "EXPLAINABILITY_API_KEY=[redacted]" still tells a reader a key was in play,
    # which a bare marker does not. A value in quotes is removed to the quote that closes it
    # (an escaped quote inside it does not: :data:`_QUOTED_VALUE`), and where the text is JSON
    # inside a string (a tool call's arguments are one) the quotes are escaped: ``PASSWORD=\"…\"``.
    (
        re.compile(
            r"(?i)" + _NAME_START + "(" + _SECRET_NAME + ")"
            r"(\s*[:=]\s*)"
            "(" + _QUOTED_VALUE + r"""|[^\s"'\n]{4,}+)"""
        ),
        r"\1\2" + MARKER,
    ),
    # The same with the NAME QUOTED, as in JSON (``{"password": "…"}``, ``"api_key":"…"``): the
    # quote that closes the name is part of the separator, and a backslash before it where the
    # JSON sits inside a string (``{\"password\": \"…\"}``). Only a QUOTED value is taken, on
    # the same line: in JSON a secret is a string, and ``"input_tokens": 12`` is not one, nor
    # is the next line after ``== "token":`` in code.
    (
        re.compile(
            r"(?i)" + _NAME_START + "(" + _SECRET_NAME + ")"
            r"""(\\*+["'][ \t]*[:=][ \t]*)"""
            "(" + _QUOTED_VALUE + ")"
        ),
        r"\1\2" + MARKER,
    ),
    # Authorization: Bearer <token> / Authorization: Basic <blob>
    (
        re.compile(
            r"(?i)" + _START + r"(authorization\s*:\s*|bearer\s+|basic\s+)"
            r"([A-Za-z0-9_.+/=~-]{12,}+)"
        ),
        r"\1" + MARKER,
    ),
    # user:password@host in a URL — the password is the point, but the whole
    # pair identifies the account, so both go.
    (re.compile(r"(?<=://)[^/\s:@]++:[^/\s:@]++@"), MARKER + "@"),
    # JWT: three base64url segments. Distinctive enough to match on shape. It starts at the
    # start of a run of its alphabet (the character before is outside it), or after a hyphen
    # inside one: ``token-eyJ…`` and ``Bearer-eyJ…`` are a prefix and a JWT, and are kept as
    # the first and removed as the second. The hyphens are walked, not tried: a start at every
    # ``eyJ`` after a hyphen would read the rest of its run each time (``eyJ-eyJ-eyJ-…`` is
    # quadratic, which a ``\b`` start was), so the run is entered once, its prefix is skipped
    # piece by piece without giving anything back, and what is read once is not read again.
    (
        re.compile(
            r"(?<![A-Za-z0-9_-])((?:(?!eyJ)[A-Za-z0-9_]*+-)*+)"
            r"eyJ[A-Za-z0-9_-]{8,}+\.[A-Za-z0-9_-]{8,}+\.[A-Za-z0-9_-]{8,}+"
        ),
        r"\1" + MARKER,
    ),
    # Vendor-prefixed tokens. Prefixes chosen because they are issued, not
    # written: none of them occur in English or in code identifiers.
    (re.compile(_START + r"sk-[A-Za-z0-9_-]{16,}+"), MARKER),
    (re.compile(_START + r"gh[pousr]_[A-Za-z0-9]{16,}+"), MARKER),
    (re.compile(_START + r"github_pat_[A-Za-z0-9_]{16,}+"), MARKER),
    (re.compile(_START + r"glpat-[A-Za-z0-9_-]{16,}+"), MARKER),
    (re.compile(_START + r"xox[baprse]-[A-Za-z0-9-]{10,}+"), MARKER),
    (re.compile(_START + r"xapp-[A-Za-z0-9-]{10,}+"), MARKER),
    (re.compile(_START + r"AKIA[0-9A-Z]{16,}+"), MARKER),
    # Our own sign-in tokens (services/iam.py). Issued, never typed by hand.
    (re.compile(_START + r"aisqr?_[A-Za-z0-9_-]{16,}+"), MARKER),
    # A RANGE, not the documented exact length. The published Google key shape
    # is AIza + 35, and a pattern pinned to 35 misses a key that is 36 long —
    # which is exactly how a scrubber passes its tests and leaks in the field.
    # The prefix is what makes this specific; the length is only a floor.
    (re.compile(_START + r"AIza[0-9A-Za-z_-]{30,}+"), MARKER),
    (re.compile(_START + r"wk_(?:live|test)_[A-Za-z0-9]{8,}+"), MARKER),
)

#: A private-key block, the shape of one: ``-----BEGIN … PRIVATE KEY-----`` to the
#: ``-----END … PRIVATE KEY-----`` that follows. Not applied with ``sub``: see
#: :func:`_without_pem_blocks`, which also takes a header with no footer after it to the end
#: of the text. Kept as one pattern for whoever asks whether text holds a whole block.
PEM_BLOCK = re.compile(
    r"-----BEGIN [A-Z ]{0,32}PRIVATE KEY-----.*?-----END [A-Z ]{0,32}PRIVATE KEY-----",
    re.DOTALL,
)
_PEM_BEGIN = re.compile(r"-----BEGIN [A-Z ]{0,32}PRIVATE KEY-----")
_PEM_END = re.compile(r"-----END [A-Z ]{0,32}PRIVATE KEY-----")

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

    One string at a time: a credential split across two (``Bearer`` ending one content block
    and its token starting the next) is whole in neither, and neither is scrubbed (r24 12).
    """
    if level is RedactionLevel.off:
        return text
    try:
        # PEM blocks first: they contain base64 that later rules would shred into
        # a dozen markers, losing the fact that it was ONE key.
        scrubbed = _without_pem_blocks(text)
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


def _without_pem_blocks(text: str) -> str:
    """``text`` with each private-key block (:data:`PEM_BLOCK`) replaced by :data:`MARKER`,
    and a header with no footer after it replaced, and everything after it, by one.

    Done in one pass over the BEGIN lines and the END lines, each found once, so the cost is
    linear in the text: ``BEGIN .*? END`` as a regular expression reads to the end of the
    text from every BEGIN that has no END after it, and a paste of 7,000 headers and no
    footer took 16 s in 400 KB (tester on #15), on the hook path.

    A BEGIN with no END after it is a block cut short, and what follows it is the key:
    ``head -3 id_rsa``, a paste clipped, a tool result cut at the transcript's bound, or a
    prompt cut at the contract's (:func:`aisquare.services.ci_augment.outbound_prompt`) all
    leave a header and the first lines of a body. Taking only the header out would ship the
    body, so the text goes from the header to its end: more than the key when prose follows
    it, and never less. No later BEGIN has an END either, so the pass ends there."""
    if "-----BEGIN " not in text:
        return text
    ends = [found.span() for found in _PEM_END.finditer(text)]
    kept: list[str] = []
    at = 0
    nearest = 0
    for begin in _PEM_BEGIN.finditer(text):
        # `span()`, not `start()` and `end()`: the hook path is held to the names it calls by
        # tests/test_remote_names_stay_off_the_hook_graph.py, which reads a `.end()` as a call
        # of the remote control's own `end`.
        opens, closes = begin.span()
        if opens < at:
            continue  # inside a block already taken out
        nearest = bisect_left(ends, (closes, 0), nearest)
        kept += [text[at:opens], MARKER]
        if nearest == len(ends):
            at = len(text)  # a header and no footer after it: the rest of the text is its body
            break
        at = ends[nearest][1]
    kept.append(text[at:])
    return "".join(kept)
