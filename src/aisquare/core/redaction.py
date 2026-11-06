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
``test_ordinary_prose_is_returned_unchanged`` is the guard against creep. Where a
shape cannot tell, SECURITY COMES FIRST (R9k, reopen 2): a value under a quoted name
that mentions a token, a secret, a password or a key is removed even when it is not
one, and a key's lines are taken past whatever prefix a listing printed before them.

Every pattern is linear-time by construction — possessive runs, no nested quantifiers,
and a start only where its token could start (see :data:`_CREDENTIAL_RULES`) — because this
runs on the primary path and a hook that hangs is a session that hangs.
"""

from __future__ import annotations

import re
from bisect import bisect_left
from collections.abc import Callable

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


#: A quoted value that ``core.insights``' clip cut before the quote that closes it: an opening
#: quote and 4 or more characters, then the clip's ``…`` (``{"password": "hunter2… [truncated
#: by aisquare-cli]``). The value is taken to the ``…``, and the clip's notice stays (R9k, after
#: reopen 2). The other quote is a character of the value (``"it's…``); its own quote, escaped
#: or not, ends its run, so it reads no further than to the next one: through its escaped
#: quotes, every name in ``token=\"ab\"`` x N read to the end of the text.
def _clipped(quote: str) -> str:
    """A value opened by ``quote`` and cut by the clip (:data:`_CLIPPED_VALUE`)."""
    char = "[^" + quote + "\\\\\n\u2026]"
    escape = "\\\\++[^" + quote + "\n\u2026]"
    return (
        "\\\\*+" + quote + "(?=(?:" + char + "|" + escape + "){4})"
        + "(?:" + char + "++|" + escape + ")*+\\\\*+(?=\u2026)"
    )  # fmt: skip


_CLIPPED_VALUE = _clipped('"') + "|" + _clipped("'")


#: A value the clip cut that holds an ESCAPED quote (R9k, reopen 3, D1): ``"abcdefghij\"klmn…
#: [truncated by aisquare-cli]``. The forms below close it at the escaped quote, the first quote,
#: and what the clip left after it shipped. So this form comes first. It reads a quote escaped
#: one level in from its own as a character of the value (``\"`` in JSON, ``\\\"`` in JSON
#: inside a string: twice its own backslashes and one), to the clip's ``…`` with only its notice
#: after it, at the end of the text. Any other quote ends its run, so it reads each character
#: once: the ``"`` of a value that closes, and the quotes of the values after it, stop it
#: (``token=\"ab\"`` x N).
def _clipped_holding_quotes(quote: str, name: str) -> str:
    """A value opened by ``quote``, the opening's backslashes kept as ``name``."""
    char = "[^" + quote + "\\\\\n\u2026]"
    held = "(?P=" + name + ")(?P=" + name + ")\\\\" + quote
    escape = "\\\\++[^" + quote + "\n\u2026]"
    return (
        "(?P<" + name + ">\\\\*+)" + quote
        + "(?=(?:" + char + "|" + held + "|" + escape + "){4})"
        + "(?:" + char + "++|" + held + "|" + escape + ")*+"
        + "\\\\*+(?=\u2026[^\"'\\r\\n]*+\\Z)"
    )  # fmt: skip


_CLIPPED_HOLDING_QUOTES = (
    _clipped_holding_quotes('"', "cutd") + "|" + _clipped_holding_quotes("'", "cuts")
)

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
    _CLIPPED_HOLDING_QUOTES
    + r'|\\"(?:[^"\\\n]|\\\\\\"|\\\\\\\\|\\\\[^"\\\n]){4,}+\\"'
    + r'|\\++"[^"\n]{4,}+"'
    + r'|"(?:[^"\\\n]|\\[^\n]){4,}+"'
    + _VALUE_END
    + r'|"[^"\n]{4,}+"'
    + r"|'(?:[^'\\\n]|\\[^\n]){4,}+'"
    + _VALUE_END
    + r"|'[^'\n]{4,}+'"
    + "|" + _CLIPPED_VALUE
)  # fmt: skip


def _cased(words: tuple[str, ...]) -> str:
    """``words`` as an alternation of how a name writes them: ``prod``, ``Prod`` and ``PROD``,
    the longest first, so a word is never read as a shorter one it starts with."""
    forms = {form for word in words for form in (word, word.capitalize(), word.upper())}
    return "(?:" + "|".join(sorted(forms, key=lambda form: (-len(form), form))) + ")"


def _camel(words: tuple[str, ...]) -> str:
    """``words`` as a camelCase segment writes them: ``Prod`` and ``PROD``."""
    forms = {form for word in words for form in (word.capitalize(), word.upper())}
    return "(?:" + "|".join(sorted(forms, key=lambda form: (-len(form), form))) + ")"


#: Segments that may follow ``auth`` and leave the name a credential's: a version (``auth2``), an
#: environment (``auth_prod``), or the form the value takes (``auth_b64``, ``authValue``).
_NAME_SUFFIXES = (
    "value", "val", "base", "b64", "base64", "hex", "raw", "plain", "plaintext", "str", "string",
    "binary", "bin", "bytes", "blob", "enc", "encoded", "encrypted", "hash", "hashed", "digest",
    "confirm", "confirmation", "prod", "production", "dev", "development", "staging", "stage",
    "test", "live", "local", "sandbox",
)  # fmt: skip

#: Where a segment starts: where the name does, or after a ``_``, ``-``, ``.`` or a quote.
_SEGMENT = r"(?<![A-Za-z0-9])"
#: Where a camelCase segment starts: an upper-case letter after a lower-case one or a digit.
_CAMEL = r"(?<=[a-z0-9])"

#: ``auth`` as the segment that ends a quoted name (R9k): Docker's ``config.json`` keeps a
#: registry's login under ``"auth"``, and ``basic_auth``, ``basicAuth`` and ``x-auth`` hold one.
#: Not a word of the rule's name (:data:`_SECRET_NAME`) anywhere in a name, as the others are:
#: ``author``, ``oauth``, ``authority`` and ``authProvider`` name no credential. The part of the
#: name before it stays in the output as it was.
_AUTH_NAME = (
    "(?=[aA])(?:" + _SEGMENT + "(?:auth|Auth|AUTH)|" + _CAMEL + "(?:Auth|AUTH))"
    + "(?:[_.-](?:" + _cased(_NAME_SUFFIXES) + "|[vV]?[0-9]++)"
    + "|" + _camel(_NAME_SUFFIXES) + "|V[0-9]++|[0-9]++)*+"
)  # fmt: skip

#: Quoted names that hold a credential's word and are documented as something else. These, and
#: only these, keep their value by NAME (R9k, reopen 2): each whole, and in the case its API
#: writes it, so ``page_token`` and ``PAGE_TOKEN`` (a Facebook Page's access token is kept under
#: them) are not ``pageToken``, and ``usage.max_tokens`` is not ``max_tokens``.
_NOT_CREDENTIALS = (
    # how many tokens a model may write: Anthropic's Messages API and OpenAI's (max_tokens),
    # OpenAI's Chat Completions (max_completion_tokens), and OpenAI's Responses API and
    # Google's Gemini (max_output_tokens)
    "max_tokens", "max_completion_tokens", "max_output_tokens",
    # OAuth 2.0's token response (RFC 6749, 5.1): the token's kind, "Bearer"
    "token_type",
    # how many tokens a text holds, as a model's usage record counts them
    "token_count",
    # Hugging Face's tokenizer_config.json and a model's config: which tokenizer, and its class
    "tokenizer", "tokenizer_class",
    # where a paged list goes on: AWS's APIs and CLI (NextToken), Amazon S3's ListObjectsV2
    # (ContinuationToken, NextContinuationToken), Azure's SDKs (continuationToken), Google's
    # APIs (pageToken, nextPageToken), Google Calendar's (syncToken, nextSyncToken), Google
    # Drive's changes (startPageToken), and Microsoft Graph's delta queries and pages
    # (deltaToken, skipToken)
    "NextToken", "ContinuationToken", "NextContinuationToken", "continuationToken",
    "pageToken", "nextPageToken", "syncToken", "nextSyncToken", "startPageToken",
    "deltaToken", "skipToken",
    # the rules a new password must meet: Auth0's connection options (password_policy)
    "password_policy",
    # where a token is asked for, not the token: an OAuth client's settings (auth_token_url),
    # Google's OAuth client file, client_secret.json (token_uri, auth_uri), OpenID Connect's
    # discovery document, a URL (token_endpoint), and its client registration (RFC 7591), how
    # the client signs in (token_endpoint_auth_method)
    "auth_token_url", "token_uri", "auth_uri", "token_endpoint", "token_endpoint_auth_method",
    # Claude Code's settings.json: the path of a script that prints the key, never the key
    "apiKeyHelper",
)  # fmt: skip

#: A quoted name that takes its string value: one that holds a credential's word anywhere, as the
#: rule for an unquoted name reads it (:data:`_SECRET_NAME`, case-insensitive), or ends in
#: ``auth`` (:data:`_AUTH_NAME`), and is not one of :data:`_NOT_CREDENTIALS` (R9k, reopen 2).
_QUOTED_CREDENTIAL_NAME = (
    "(?i:" + _NAME_START
    + "(?!(?<=[\"'])(?-i:" + "|".join(_NOT_CREDENTIALS) + ")" + r"""\\*+["'])"""
    + _SECRET_NAME + ")"
    + "|" + _AUTH_NAME
)  # fmt: skip

#: npm's dist-tags, which a ``package.json`` may give for a version: ``"next-auth": "beta"``.
_DIST_TAGS = ("latest", "next", "beta", "alpha", "canary", "rc", "experimental", "nightly")
#: A version, a prerelease named as one (``-beta.1``, ``-rc.2``, ``-canary-6bc0e3f0``), and a range
#: of them (``^4.24.5``, ``~4.0``, ``>=1.0.0 <2``, ``2.x || 3.x``, ``1.0.0 - 2.0.0``). The first
#: version has a dot: ``12345678`` is no range.
_PRERELEASE = (
    "(?:-(?:alpha|beta|rc|next|canary|pre|preview|dev|experimental|nightly|insiders|[0-9]++)"
    "(?:[.-][0-9A-Za-z]++)*+)?"
)
_COMPARATOR = "(?:[~^]|[<>]=?|=)?[ \\t]*+"
_VERSION_ANY = "v?[0-9]++(?:\\.(?:[0-9]++|[xX*])){0,2}" + _PRERELEASE
_RANGE = (
    _COMPARATOR + "v?[0-9]++(?:\\.(?:[0-9]++|[xX*])){1,2}" + _PRERELEASE
    + "(?:(?:[ \\t]++-[ \\t]++|[ \\t]*+\\|\\|[ \\t]*+|[ \\t]++)"
    + _COMPARATOR + _VERSION_ANY + ")*+"
)  # fmt: skip
_NPM_NAME = "(?:@[a-z0-9][a-z0-9._-]*+/)?[a-z0-9][a-z0-9._-]*+"
_REF = "#[A-Za-z0-9_./-]++"
_PATH = "(?:\\.{1,2}/|/|~/|[A-Za-z0-9_.@-]++/)[A-Za-z0-9_./@~+-]*+"
#: Each shape a package's spec takes, as the WHOLE value (R9k, reopen 3, N4): a range; a
#: dist-tag; ``workspace:*`` or a range; ``npm:`` and a name, then ``@`` and a range or a tag;
#: ``github:``, ``gitlab:`` or ``bitbucket:`` and owner/repo, with a ref; ``file:``, ``link:`` or
#: ``portal:`` and a path; ``catalog:`` and a name; ``patch:`` with its version and patch file.
#: Anything after such a start is a secret's, and so is a value that only starts so
#: (``1.2hunter2``, ``github:hunter2``). NO URL (R9k, reopen 5, S2): a git, tarball or ``#ref``
#: URL under a credential's name can carry a token in its user or its path (Gemfury's
#: ``https://npm-proxy.fury.io/<token>/…``), and is taken: an over-match accepted (P3).
_SPEC_SHAPES = "|".join((
    _RANGE,
    "(?:" + "|".join(_DIST_TAGS) + ")",
    "workspace:(?:[*^~]|" + _COMPARATOR + _VERSION_ANY + ")",
    "npm:" + _NPM_NAME + "@(?:" + _COMPARATOR + _VERSION_ANY + "|" + "|".join(_DIST_TAGS) + ")",
    "(?:github|gitlab|bitbucket):[A-Za-z0-9_.-]++/[A-Za-z0-9_.-]++(?:" + _REF + ")?",
    "(?:file|link|portal):" + _PATH,
    "catalog:[A-Za-z0-9_.-]*+",
    "patch:" + _NPM_NAME + "@[A-Za-z0-9_.%:^~<>=*-]++#" + _PATH,
))  # fmt: skip

#: A value that is a package's spec, not a secret (:data:`_SPEC_SHAPES`), WHOLE: to a closing
#: quote with as many backslashes before it as its opening quote, as a placeholder is.
_PACKAGE_SPEC = (
    r'(?:(?P<spd>\\*+)"(?:' + _SPEC_SHAPES + r')(?P=spd)"'
    + r"|(?P<sps>\\*+)'(?:" + _SPEC_SHAPES + r")(?P=sps)')"
    + r"(?=[\s,;}\])\\]|\Z)"
)  # fmt: skip

#: A whole value that stands for one and is not one: a placeholder (``<YOUR_API_KEY>``), a
#: model's special tokens (``</s>``, ``<unk>``, ``[CLS]``, ``[MASK]``), a template's bare reference
#: (``${localEnv:GITHUB_TOKEN}``, ``${{ secrets.NPM_TOKEN }}``, ``{{access_token}}``), and
#: ``true``, ``false``, ``null`` and Python's ``None``. No issued credential is written so. A
#: reference with a literal default or a filter's arguments holds a value, and is one: a shell's
#: ``${PGPASS:-…}``, ``${VAR=…}``, devcontainer's ``${localEnv:PGPASS:…}``, Jinja's ``{{ pw |
#: default(…) }}`` (R9k, after reopen 2, N1).
_REFERENCE = (
    r"[ \t]*+(?:[A-Za-z_][A-Za-z0-9_]*+:)?[A-Za-z_][A-Za-z0-9_]*+(?:\.[A-Za-z_][A-Za-z0-9_]*+)*+"
    r"[ \t]*+"
)
_PLACEHOLDER_BODY = (
    r"""<[^"'\\<>\n]*+>"""
    + r"|\$\{\{" + _REFERENCE + r"\}\}"
    + r"|\$\{" + _REFERENCE + r"\}"
    + r"|\{\{" + _REFERENCE + r"\}\}"
    + r"|\[[A-Z][A-Z0-9_]*+\]"
    + r"|true|false|null|True|False|None|TRUE|FALSE|NULL"
)  # fmt: skip
#: The whole value, to a closing quote with as many backslashes before it as the opening quote
#: has: ``"<a>\" …"`` and ``"null\", …"`` hold more after the placeholder (N3).
_PLACEHOLDER = (
    r'(?:(?P<dq>\\*+)"(?:' + _PLACEHOLDER_BODY + r')(?P=dq)"'
    + r"|(?P<sq>\\*+)'(?:" + _PLACEHOLDER_BODY + r")(?P=sq)')"
    + r"(?=[\s,;}\])\\]|\Z)"
)  # fmt: skip


def _in_its_quotes(found: re.Match[str]) -> str:
    """A quoted name's value replaced INSIDE its quotes: ``"password": "[redacted]"``, so JSON
    stays JSON, as 0.9.2 wrote a token it took from inside a value (R9k, reopen 2). The quotes
    are the value's own, with the backslashes before them where the JSON sits in a string."""
    name, separator, value = found["name"], found["sep"], found["value"]
    opening = len(value) - len(value.lstrip("\\")) + 1
    cut = found.string.startswith("\u2026", found.span("value")[1])  # the clip's, after it
    if cut or len(value) <= opening or value[-1] != value[opening - 1]:
        return name + separator + value[:opening] + MARKER  # cut by the clip: no closing quote
    inside = value[:-1]
    closing = 1 + min(opening - 1, len(inside) - len(inside.rstrip("\\")))
    return name + separator + value[:opening] + MARKER + value[-closing:]


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
_CREDENTIAL_RULES: tuple[tuple[re.Pattern[str], str | Callable[[re.Match[str]], str]], ...] = (
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
    # is the next line after ``== "token":`` in code. SECURITY FIRST (R9k, reopen 2): a name
    # that holds a credential's word anywhere takes its value (:data:`_QUOTED_CREDENTIAL_NAME`),
    # as the PR's rule did, and a name that only mentions one (``"login.password":
    # "Password"``, ``"gen-token": "node …"``) loses an ordinary value with it: fidelity lost in
    # a feature that is off by default, never a secret shipped. The only values kept are a
    # documented field's (:data:`_NOT_CREDENTIALS`), a package's spec (:data:`_PACKAGE_SPEC`)
    # and a value that stands for one (:data:`_PLACEHOLDER`). The value's quotes stay.
    (
        re.compile(
            "(?P<name>" + _QUOTED_CREDENTIAL_NAME + ")"
            r"""(?P<sep>\\*+["'][ \t]*[:=][ \t]*)"""
            "(?!" + _PACKAGE_SPEC + "|" + _PLACEHOLDER + ")"
            "(?P<value>" + _QUOTED_VALUE + ")"
        ),
        _in_its_quotes,
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

#: A key's body, base64, as the lines after a header with no footer hold it. ``_BASE64`` is its
#: alphabet; a line break is a real one, an escaped one (``\n`` where the key sits in a JSON or a
#: code string) or HTML's ``<br>``; between two runs on a line may stand spaces, quotes, ``+`` and
#: ``,`` (a key written as a string concatenation, a line quoted with ``>``).
_BASE64 = r"[A-Za-z0-9+/=]"
_BASE64_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
_LINE_BREAK = r"(?:\r\n?|\n|\\+[rn]|<[bB][rR][ \t]*+/?>)"
_GAP = r"""(?:\\+["']|[ \t"'+,>])"""
#: The run that is a key's whatever stands before it: no word of prose is this long.
_BODY_RUN = 20
#: The body of a key after a header with no footer: what :func:`_without_pem_blocks` takes with
#: the header, run by run, and no further. A run is the key's when it is :data:`_BODY_RUN`
#: characters or longer; when the ``…`` of a clip follows it, or half an escape and the ``…``
#: (``core.insights`` clips before it scrubs); when a line, or a string, is that run alone (the
#: last, short line of a key, a body that ``head`` or the end of the text cut; in HTML, the run
#: before a tag, or before what the clip left of one, ``<b…``); when it ends the
#: text after a long run on its line; and the RFC 1421 lines of a legacy encrypted key
#: (``Proc-Type:``, ``DEK-Info:``). The first run that is none of these, a word of prose after the
#: header, ends it: ``grep '-----BEGIN RSA PRIVATE KEY-----' finds nothing`` has no body, and is
#: kept whole. Possessive throughout, and each run is read once: linear.
#: A long run is a key's only if it reads like base64 data: mixed case, a digit, ``+`` or ``=``.
#: ``internationalization`` and ``/home/me/projects/deploy/keys`` after a header are prose. A
#: random base64 run of 20 characters has none of those with a chance of 2 x (27/64)^20, about
#: 6e-8 (one of 64 characters, about 1e-24), and the short runs (a line alone, a cut) need no test.
#: An identifier of 20 letters or more in both cases that starts the line after a header
#: (``AuthenticationFailedException: …``) reads as one, and goes with the header: accepted.
_LONG_RUN = (
    "(?![a-z/]*+(?![A-Za-z0-9+/=]))(?![A-Z/]*+(?![A-Za-z0-9+/=]))"
    + _BASE64 + "{" + str(_BODY_RUN) + ",}+"
)  # fmt: skip
_CUT_RUN = _BASE64 + "++(?=\\\\*+\u2026)"
#: A listing's gutter before each line, read by its number (R9k, reopen 2): ``cat -n`` and
#: ``nl`` (``     2\t``, ``     2  ``), ``less -N`` (``      2 ``), Claude Code's Read tool
#: (``     2→``), an editor's or ``bat``'s (``  2 | ``, ``  2 │ ``), and ``grep -n`` reading one
#: file (``2:``, and ``2-`` for a line of context).
_NUMBER_GUTTER = "[ \\t]*+[0-9]++(?:[ \\t]*+[\u2192|\u2502]|[ \\t]++|[:-])" + _GAP + "*+"
#: ``grep`` over files: the file's name as the header's line gave it, then the line's number
#: (``id_rsa:2:``), or none (``id_rsa:``, without ``-n``); ``-`` for a line of context. Another
#: file's line (``./b.sh:12:``) is not the key's.
_FILE_GUTTER = "(?P=file)[:-](?:[0-9]++[:-])?" + _GAP + "*+"


def _body_run(gutter: str) -> str:
    """One run of a key's body, with what stands before it. ``gutter`` is what may stand at the
    start of each of the key's lines before its run, where a listing printed one."""
    return (
        "(?:"
        # a run that starts a line
        + _GAP + "*+" + _LINE_BREAK + "(?:" + _GAP + "|" + _LINE_BREAK + ")*+"
        + ("(?:" + gutter + ")?+" if gutter else "")
        + "(?:(?:Proc-Type|DEK-Info):[^\\r\\n\\\\\"'<\u2026]*+"
        + "|" + _LONG_RUN
        + "|" + _CUT_RUN
        + "|" + _BASE64 + "++(?=" + _GAP + "*+(?:" + _LINE_BREAK + "|<|\\\\*+\\Z)|\\\\*+[\"'])"
        + ")"
        # a run on the header's line, or on the line of a run before it
        + "|" + _GAP + "*+(?:" + _LONG_RUN
        # and a short run after it that ends the text, or a string (a JSON array's last line)
        + "(?:" + _GAP + "*+(?!" + _LONG_RUN + ")" + _BASE64 + "++(?=\\\\*+[\"']|\\Z))?"
        + "|" + _CUT_RUN + ")"
        + ")"
    )  # fmt: skip


#: One run of a key's body, with what stands before it, where no gutter stands before its lines.
_PEM_BODY_RUN = _body_run("")
_PEM_BODY = re.compile(_PEM_BODY_RUN + "*+")
#: The same where the header's line has a number before it (:data:`_NUMBER_GUTTER`).
_PEM_BODY_NUMBERED = re.compile(_body_run(_NUMBER_GUTTER) + "*+")
#: The same where it has a file's name before it (:data:`_FILE_GUTTER`): read from the name on.
_PEM_BODY_FILED = re.compile(
    "(?P<file>[^\\s:\"'\\\\>]{1,255}?)[:-](?:[0-9]++[:-])?" + _GAP + "*+"
    + _PEM_BEGIN.pattern + _body_run(_FILE_GUTTER) + "*+"
)  # fmt: skip
#: The rest of a line, to its break (as :data:`_LINE_BREAK` reads one) or to a clip's ``…``.
_LINE_REST_PATTERN = "(?:[^\\r\\n\\\\<\u2026]++|\\\\++(?![rn])|<(?![bB][rR][ \\t]*+/?>))*+"
#: One character of a line, a clip's or an ellipsis's ``…`` too: ``journalctl`` cuts a long line
#: with one, and the key's next line follows it.
_IN_LINE = "(?:[^\\r\\n\\\\<]|\\\\++(?![rn])|<(?![bB][rR][ \\t]*+/?>))"
#: A run starts here: not inside another.
_EDGE = "(?<![A-Za-z0-9+/=])"
#: What may end a key's line after its run: ``cat -A``'s and ``sed -n l``'s ``$``, ``^M$`` for a
#: CRLF line, a table's `` |``. Not a ``-``: ``Proc-…`` is an RFC 1421 label the clip cut.
_LINE_SUFFIX = "[^A-Za-z0-9^\\-\\r\\n\\\\<\u2026]*+(?:\\^M)?[^A-Za-z0-9\\-\\r\\n\\\\<\u2026]*+"
#: A line of a key's body WHATEVER WRAPS IT (R9k, after reopen 2, N2): ``cat -A``'s ``$``, ``git
#: blame``'s or ``journalctl``'s prefix that changes on every line, a table's pipes. A line with
#: a run that is a key's (:data:`_LONG_RUN`) anywhere on it, to its last; or, where the key ends,
#: a line whose last run reads as base64 (four characters or more) with nothing but a suffix
#: after it, or what the clip left of one; or a run the clip cut. Not a word of prose that ends
#: a line, nor a path: neither reads as base64 data. An identifier in both cases does, and goes
#: with the key: accepted.
_WRAPPED_LINE = (
    _LINE_BREAK
    + "(?:"
    + "(?:" + _IN_LINE + "*?" + _EDGE + _LONG_RUN + ")++"
    + "|" + _IN_LINE + "*?" + _EDGE
    + "(?![a-z/]*+(?![A-Za-z0-9+/=]))(?![A-Z/]*+(?![A-Za-z0-9+/=]))" + _BASE64 + "{4,}+"
    + "(?=" + _LINE_SUFFIX + "(?:" + _LINE_BREAK + "|\\\\*+\\Z|\u2026))"
    + "|" + _IN_LINE + "*?" + _EDGE + _CUT_RUN
    + ")"
)  # fmt: skip
#: The rest of a line, ``…`` and all, to its break: the match ends at a run's end, so a clip's
#: notice after the run stays outside it.
_TO_LINE_END = "(?:[^\\r\\n\\\\<]++|\\\\++(?![rn])|<(?![bB][rR][ \\t]*+/?>))*+"
#: The most text that may stand between two runs of one key that no reader here knows as a line
#: break: ``&#13;&#10;``, ``</div><div>`` and ``\u000a`` are shorter, and a sentence is longer.
_SEPARATION = 24
#: MARKUP between a key's runs, which does not count against :data:`_SEPARATION` (R9k, reopen 5,
#: S1): a tag of up to 256 characters with its attributes (VS Code's and Google Docs' HTML, a
#: highlighted code block's spans, Slack's divs, a table row's cells), a character reference
#: (``&#10;``, ``&nbsp;``), and whitespace (a pretty-printed attribute's indent, the newlines
#: between GitHub's blob rows). A word of prose outside a tag still counts.
_MARKUP = "(?:<[^<>]{0,256}+>|&#?[A-Za-z0-9]{1,10}+;|\\s)"
#: The most a gap may hold, markup and all: past it the key has ended, whatever follows.
_GAP_CAP = 512
#: Not the clip's notice at the end of the text (``… [truncated by aisquare-cli]``): another
#: ``…`` between two runs (an ellipsis a reader printed) is a character of the gap, and the
#: clip's ends it (reopen 5, MV2).
_NOT_THE_CLIP = "(?!\u2026 \\[truncated by [^\\]\\r\\n]{0,32}+\\]\\Z)"


def _gap(stop: str) -> str:
    """What may stand before a piece of a key after its body: markup at any length, and at most
    :data:`_SEPARATION` characters of anything else, never the clip's notice; it ends where
    ``stop`` starts. Possessive throughout, each character read once."""
    return (
        "(?:" + _MARKUP + "*+(?!" + stop + ")" + _NOT_THE_CLIP + "[\\s\\S]){0,"
        + str(_SEPARATION) + "}+" + _MARKUP + "*+"
    )  # fmt: skip


#: A key's run after a SEPARATOR this reader does not know (R9k, reopen 4, K1): ``&#10;`` and
#: ``&#xD;&#xA;`` in an XML attribute, ``</div><div>`` from a rich-text editor, U+2028, U+0085,
#: a form feed or a vertical tab, ``\u000a``, ``\012``, ``%0A``, and those no one has thought of.
#: Security first: after a header, a run that reads as a key's (:data:`_LONG_RUN`) after a gap
#: (:func:`_gap`) is the key's. Prose between two runs is longer than the gap allows.
_SEPARATED_RUN = _gap(_EDGE + _LONG_RUN) + _EDGE + _LONG_RUN
#: Where a key ends after such a separator: its short last run (four characters or more that
#: read as base64, no word after them), or a run the clip cut. The first only after a run was
#: taken, where prose after a header would be read as one; the second right after the header
#: too (:data:`_SEPARATED_CUT`), where the clip cut the key's first run short.
_LAST_RUN = (
    "(?:(?![a-z/]*+(?![A-Za-z0-9+/=]))(?![A-Z/]*+(?![A-Za-z0-9+/=]))" + _BASE64 + "{4,}+"
    + "(?![ \\t]++[A-Za-z])|" + _CUT_RUN + ")"
)  # fmt: skip
_SEPARATED_END = _gap(_EDGE + _LAST_RUN) + _EDGE + _LAST_RUN
#: One more piece of a key after its body: a run after a separator, or a line whatever wraps it.
_TAIL_ITEM = "(?:" + _SEPARATED_RUN + "|" + _TO_LINE_END + _WRAPPED_LINE + ")"
_A_SEPARATED_RUN = re.compile(_SEPARATED_RUN)
_SEPARATED_CUT = _gap(_EDGE + _CUT_RUN) + _EDGE + _CUT_RUN
_A_SEPARATED_END = re.compile(_SEPARATED_END)
_A_SEPARATED_CUT = re.compile(_SEPARATED_CUT)
_A_WRAPPED_LINE = re.compile(_WRAPPED_LINE)
#: A header with no footer and a body after it, as one pattern: what the linear pass removes for
#: one at the start of its line, for whoever asks it of a text (the tests' reference does). A
#: header with NO body after it (``grep`` quoting one, a doc naming one, the clip cutting right
#: after its line) holds nothing secret, and is kept, as 0.9.2 kept it (R9k, H1).
_PEM_HEADER_ALONE = re.compile(
    _PEM_BEGIN.pattern
    + "(?:(?:" + _PEM_BODY_RUN + "++" + _TAIL_ITEM + "*+|" + _TAIL_ITEM + "++)"
    + "(?:" + _SEPARATED_END + ")?|" + _SEPARATED_CUT + ")"
)  # fmt: skip

#: How far before a header the start of its line is looked for. Farther, and what stands before
#: the header is a sentence, not a gutter: its body is read as one with no gutter.
_PREFIX_REACH = 256
#: What :data:`_GAP` is made of, for the ends of a line's prefix.
_GAP_CHARS = " \t\"'+,>\\"
_A_LINE_BREAK = re.compile(_LINE_BREAK)
#: A prefix that ends in a number and its sign: ``     2\t``, ``     2→``, ``  2 | ``, ``2:``.
_NUMBERED_PREFIX = re.compile("(?:^|(?<=[\\s\"'>]))" + _NUMBER_GUTTER + "\\Z")
#: A prefix's last word that is a file's name and a line's number: ``id_rsa:2:``, ``id_rsa:``.
_FILED_PREFIX = re.compile("[^\\s:\"'\\\\>]{1,255}?[:-](?:[0-9]++[:-])?")
_LINE_REST = re.compile(_LINE_REST_PATTERN)

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
    and a header with no footer after it replaced, with its key's body (:data:`_PEM_BODY`), by
    one. A header with no body after it is left as it is: it holds nothing secret.

    Done in one pass over the BEGIN lines and the END lines, each found once, so the cost is
    linear in the text: ``BEGIN .*? END`` as a regular expression reads to the end of the
    text from every BEGIN that has no END after it, and a paste of 7,000 headers and no
    footer took 16 s in 400 KB (tester on #15), on the hook path.

    A BEGIN with no END after it is a block cut short, and what follows it is the key:
    ``head -3 id_rsa``, a paste clipped, a tool result cut at the transcript's bound, or a
    prompt cut at the contract's (:func:`aisquare.services.ci_augment.outbound_prompt`) all
    leave a header and the first lines of a body. Taking only the header out would ship the
    body, so the body goes with it, to its last run, and the prose after it stays: taking the
    text to its end (as #25 did) removed the question asked about the key, and the words after
    a header that was only quoted (R9k). No later BEGIN has an END either, and each is taken
    with its own body, or kept when it has none. A key whose lines a listing numbered is read
    past each line's number (:func:`_end_of_body`)."""
    if "-----BEGIN " not in text:
        return text
    ends = [found.span() for found in _PEM_END.finditer(text)]
    kept: list[str] = []
    at = 0
    nearest = 0
    lines: _Lines | None = None  # found at the first header with no footer
    for begin in _PEM_BEGIN.finditer(text):
        # `span()`, not `start()` and `end()`: the hook path is held to the names it calls by
        # tests/test_remote_names_stay_off_the_hook_graph.py, which reads a `.end()` as a call
        # of the remote control's own `end`.
        opens, closes = begin.span()
        if opens < at:
            continue  # inside a block already taken out
        nearest = bisect_left(ends, (closes, 0), nearest)
        if nearest == len(ends):
            # A header and no footer after it: the header and its body, never what follows it.
            lines = lines or _Lines(text)
            stop = lines.tail(text, closes, _end_of_body(text, opens, closes))
            if stop > closes:  # no body, nothing secret: the header is kept, as 0.9.2 kept it
                kept += [text[at:opens], MARKER]
                at = stop
            continue
        kept += [text[at:opens], MARKER]
        at = ends[nearest][1]
    kept.append(text[at:])
    return "".join(kept)


class _Lines:
    """Where a text's line breaks are, found once (a pass over the text), and where the key's
    line after each line break ends (:data:`_WRAPPED_LINE`), read once: every header on a line
    asks it of the same line break, and 7,000 of them reading the line after theirs again each
    is the quadratic case the PEM rule is not a regular expression to avoid."""

    def __init__(self, text: str) -> None:
        self.breaks = [found.span()[0] for found in _A_LINE_BREAK.finditer(text)]
        self.read: dict[int, int | None] = {}

    def tail(self, text: str, closes: int, at: int) -> int:
        """``at``, the end of the body of the key whose header ends at ``closes``, past what
        comes after it that is still the key's (:data:`_TAIL_ITEM`, one at a time): a run after
        a separator no reader knows (:data:`_SEPARATED_RUN`), or the next line whatever wraps
        it (:data:`_WRAPPED_LINE`). Once anything was taken, its last short run or a cut one
        (:data:`_SEPARATED_END`); where nothing was, a cut one (:data:`_SEPARATED_CUT`). ``at``
        where there is none."""
        end = at
        while True:
            stop = _within_the_cap(_A_SEPARATED_RUN.match(text, end), end)
            if stop is None:
                stop = self._wrapped(text, end)
            if stop is None:
                break
            end = stop
        last = _A_SEPARATED_END if end > closes else _A_SEPARATED_CUT
        stop = _within_the_cap(last.match(text, end), end)
        return end if stop is None else stop

    def _wrapped(self, text: str, at: int) -> int | None:
        """Where the key's line after the line break that ends ``at``'s line ends, or ``None``
        where that line is not the key's or there is none (none after the clip's notice, which
        ends the text)."""
        nearest = bisect_left(self.breaks, at)
        if nearest == len(self.breaks):
            return None
        brk = self.breaks[nearest]
        if brk not in self.read:
            found = _A_WRAPPED_LINE.match(text, brk)
            self.read[brk] = found.span()[1] if found is not None else None
        return self.read[brk]


def _within_the_cap(found: re.Match[str] | None, at: int) -> int | None:
    """Where a piece of a key that ``found`` read from ``at`` ends, or ``None`` when there is
    none, or when its gap holds more than :data:`_GAP_CAP` characters, markup and all. A run is
    what its match ends in: base64, after a gap whose last character is none."""
    if found is None:
        return None
    piece = found.group()
    run = len(piece) - len(piece.rstrip(_BASE64_CHARS))
    return found.span()[1] if len(piece) - run <= _GAP_CAP else None


def _matched_to(found: re.Match[str] | None, at: int) -> int:
    """Where ``found`` ends, or ``at`` where nothing was (a pattern of ``*+`` always matches)."""
    return found.span()[1] if found is not None else at


def _end_of_body(text: str, opens: int, closes: int) -> int:
    """Where the body of the key whose header, with no footer after it, is ``text[opens:closes]``
    ends: ``closes`` where it has none.

    The body is read by what stands before the header on its line (R9k, reopen 2), because
    ``cat -n``, ``grep -n`` and Claude Code's Read tool print a key's lines each after a gutter,
    and a body read as a line that starts with base64 stopped at the first of them and shipped
    the rest: a number (``     1\t``, ``     1→``, ``  1 | ``, ``1:``) lets each line of the key
    start with a number; a file's name (``id_rsa:1:``) with that name; nothing, or only quotes,
    spaces and ``>``, with nothing. Any other prefix (``# ``, ``// ``, a diff's ``-``) cannot be
    read, and the lines right after the header that start with it go with the header, whatever
    they hold; the body goes on after them as one with no gutter. Each costs what it reads: the
    prefix, at most :data:`_PREFIX_REACH` characters back, and the lines it takes."""
    reach = max(0, opens - _PREFIX_REACH)
    starts = reach
    for found in _A_LINE_BREAK.finditer(text, reach, opens):
        starts = found.span()[1]
    if starts == reach and reach > 0:  # the line started farther back: a sentence, not a gutter
        return _matched_to(_PEM_BODY.match(text, closes), closes)
    prefix = text[starts:opens]
    line = prefix.rstrip(_GAP_CHARS)
    if not line.strip(_GAP_CHARS):
        return _matched_to(_PEM_BODY.match(text, closes), closes)
    if _NUMBERED_PREFIX.search(prefix[-32:]):
        return _matched_to(_PEM_BODY_NUMBERED.match(text, closes), closes)
    word = max(map(line.rfind, " \t\"'>\\")) + 1
    if _FILED_PREFIX.fullmatch(line, word):
        filed = _PEM_BODY_FILED.match(text, starts + word)
        if filed is not None and filed.span()[1] >= closes:
            return filed.span()[1]
    run = closes
    pos = _matched_to(_LINE_REST.match(text, closes), closes)
    lead = prefix.rstrip(" \t")
    while (brk := _A_LINE_BREAK.match(text, pos)) is not None and text.startswith(
        lead, brk.span()[1]
    ):
        run = pos = _matched_to(_LINE_REST.match(text, brk.span()[1] + len(lead)), closes)
    return _matched_to(_PEM_BODY.match(text, run), run)
