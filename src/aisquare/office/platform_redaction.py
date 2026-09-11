"""What the platform transport is allowed to say out loud, and in what shape.

Three jobs, deliberately in one small module with no serving dependency so the
fixture tests and any base-install diagnostic can use it without the ``office``
extra:

**Sanitising a detail.** Every string that reaches a ``ServiceError.detail``, a
log line or an exception message passes through :func:`sanitize_detail`. It
removes the configured credential first, then whole query strings (a key rides
in one more often than it rides in a body), then the CLI's own strict redaction
rules for the credential and identity patterns this repository already knows,
and only then bounds the result. The order matters: bounding first would clip a
key in half and leave the half.

**Allow-listing response headers.** :data:`ALLOWED_RESPONSE_HEADERS` is a list
of names, not a list of things to strip. A deny-list is wrong here because the
upstream is free to add a header tomorrow, and the failure mode of a missed
deny entry is a ``Set-Cookie`` or an echoed credential landing in a captured
fixture.

**Describing a body without quoting it.** :func:`body_shape` returns type names
and key names and *nothing else* — no lengths, no counts, no values. That is
what makes a captured fixture safe by construction rather than safe by careful
reading: there is no code path by which a prompt, an email, a run id or a key
can appear in the output, because values are never copied into it at all.

The one thing this module never does is *hash* a credential into something it
returns. Fingerprinting belongs to :mod:`aisquare.office.platform_config`,
where it stays inside the cache key and the binding revision.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Final

from aisquare.core.redaction import redact
from aisquare.models import RedactionLevel

MARKER: Final = "[redacted]"
"""What a removed fragment becomes. Same marker the CLI's redactor uses."""

MAX_DETAIL_CHARS: Final = 500
"""``ServiceError.detail``'s own bound, restated so the clip happens here."""

MIN_SECRET_FRAGMENT: Final = 8
"""Below this a fragment is too short to be a credential and too likely to be
an ordinary word: replacing every ``wk`` in a sentence would corrupt details
without protecting anything."""

ALLOWED_RESPONSE_HEADERS: Final = frozenset(
    {
        "content-type",
        "date",
        "retry-after",
        "x-request-id",
        "x-correlation-id",
        "x-ratelimit-limit",
        "x-ratelimit-remaining",
        "x-ratelimit-reset",
    }
)
"""The only response headers that survive into a :class:`TransportResponse`.

No ``set-cookie``, no ``authorization``, no ``x-api-key`` — and no
``www-authenticate``, which quotes the scheme and realm an operator does not
need and a fixture must not carry.
"""

REQUEST_ID_HEADERS: Final = ("x-request-id", "x-correlation-id")
"""Where a safe correlation id is looked for, in order of preference."""

MAX_REQUEST_ID_CHARS: Final = 128

_REQUEST_ID_OK = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_QUERY = re.compile(r"\?\S*")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
_WHITESPACE = re.compile(r"\s+")


def scrub_credential(text: str, credential: str | None) -> str:
    """``text`` with every long-enough fragment of ``credential`` removed.

    Fragments, not just the whole value, because the places this runs — an
    exception's ``str()``, a transport library's own message — are free to
    quote a header value with a line break in it or to truncate it. Longest
    first so replacing a prefix cannot leave a suffix behind.

    Lifted in shape from :func:`aisquare.services.ci_client.scrub_secret`,
    which does the same job for the CI test bed's token; the difference is that
    this one takes the credential as an argument instead of reading the
    environment, because the platform key may come from a file and because a
    function that reaches for a secret on its own is one that can be called
    somewhere it should not have been.
    """
    if not credential:
        return text
    fragments = {credential, *credential.splitlines()}
    for fragment in sorted(
        (piece.strip() for piece in fragments if len(piece.strip()) >= MIN_SECRET_FRAGMENT),
        key=len,
        reverse=True,
    ):
        text = text.replace(fragment, MARKER)
    return text


def sanitize_detail(
    text: str,
    *,
    credential: str | None = None,
    limit: int = MAX_DETAIL_CHARS,
) -> str:
    """One bounded, credential-free sentence fit for a ``ServiceError.detail``.

    Never raises: this runs on the failure path, and a sanitiser that throws
    while explaining a failure replaces a useful message with a traceback that
    may itself quote the thing being hidden.
    """
    try:
        cleaned = scrub_credential(text, credential)
        cleaned = _QUERY.sub(f"?{MARKER}", cleaned)
        cleaned = redact(cleaned, RedactionLevel.strict)
        cleaned = _CONTROL.sub(" ", cleaned)
        cleaned = _WHITESPACE.sub(" ", cleaned).strip()
    except Exception:  # pragma: no cover - defensive; redact() already swallows its own
        return MARKER
    if not cleaned:
        return MARKER
    if len(cleaned) > limit:
        return cleaned[: max(limit - 3, 1)].rstrip() + "..."
    return cleaned


def allowed_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Only :data:`ALLOWED_RESPONSE_HEADERS`, lower-cased and bounded."""
    kept: dict[str, str] = {}
    for name, value in headers.items():
        lowered = name.lower()
        if lowered in ALLOWED_RESPONSE_HEADERS:
            kept[lowered] = value[:256]
    return kept


def request_id_from(headers: Mapping[str, str]) -> str | None:
    """A safe correlation id, or None.

    Validated against a conservative character class rather than merely
    truncated: an upstream is not obliged to put an opaque token here, and an
    id that arrives carrying a URL or a quoted body is not an id.
    """
    lowered = {name.lower(): value for name, value in headers.items()}
    for candidate in REQUEST_ID_HEADERS:
        value = lowered.get(candidate, "").strip()
        if value and _REQUEST_ID_OK.match(value):
            return value
    return None


def detail_of(json_body: object, *, credential: str | None = None) -> str | None:
    """The upstream's own explanation, sanitised — or None when it gave none.

    FastAPI answers ``{"detail": ...}`` and ``detail`` is a string on most
    handlers and a structured object on some (a validation error's location
    list, a 409 that nests a whole record). Both are tolerated and neither is
    ever surfaced verbatim: the structured form is compacted to JSON and then
    sanitised exactly like the string form, so a nested prompt or path is
    removed by the same rules.
    """
    if not isinstance(json_body, dict):
        return None
    raw = json_body.get("detail")
    if raw is None:
        return None
    if isinstance(raw, str):
        text = raw
    else:
        try:
            text = json.dumps(raw, separators=(",", ":"), default=str)[: MAX_DETAIL_CHARS * 2]
        except (TypeError, ValueError):  # pragma: no cover - default=str makes this unreachable
            return None
    return sanitize_detail(text, credential=credential)


# --------------------------------------------------------------------------
# Shapes
# --------------------------------------------------------------------------

_SCALARS: Final = ("null", "boolean", "integer", "number", "string")
MAX_SHAPE_DEPTH: Final = 8
MAX_SHAPE_KEYS: Final = 64
MAX_SHAPE_SAMPLE: Final = 20
"""How many array elements are merged to learn a row's optional fields. Enough
to see a nullable column that is populated on some rows and not others, and
bounded so a thousand-row page is not walked to say the same thing."""


def _scalar_name(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return "string"


def _merge(left: object, right: object) -> object:
    """The shape that describes both ``left`` and ``right``.

    Scalars union into ``"integer|null"``; objects union their keys, and a key
    present on one side only is marked ``absent`` in the union so P13/P14 can
    see that a field is *optional* rather than merely nullable. Those are
    different facts and an adapter that conflates them writes a required field
    into a model that some rows do not have.
    """
    if left == right:
        return left
    if isinstance(left, str) and isinstance(right, str):
        return "|".join(sorted(set(left.split("|")) | set(right.split("|"))))
    if isinstance(left, dict) and isinstance(right, dict):
        flags: dict[str, object] = {}
        for flag in ("nullable", "optional"):
            if left.get(flag) or right.get(flag):
                flags[flag] = True
        left_object, right_object = left.get("object"), right.get("object")
        if isinstance(left_object, dict) and isinstance(right_object, dict):
            merged: dict[str, object] = {}
            for key in sorted(set(left_object) | set(right_object)):
                if key in left_object and key in right_object:
                    merged[key] = _merge(left_object[key], right_object[key])
                else:
                    present = left_object.get(key, right_object.get(key))
                    merged[key] = _merge(present, "absent")
            return {"object": merged, **flags}
        left_array, right_array = left.get("array"), right.get("array")
        if left_array is not None and right_array is not None:
            if left_array == "empty":
                return {**right, **flags}
            if right_array == "empty":
                return {**left, **flags}
            return {"array": _merge(left_array, right_array), **flags}
    if isinstance(left, dict) and isinstance(right, str):
        return _flagged(left, right)
    if isinstance(right, dict) and isinstance(left, str):
        return _flagged(right, left)
    return "mixed"


def _flagged(container: dict[str, object], token: str) -> object:
    """A container that is sometimes null or sometimes absent, said once.

    ``summary_counts`` is an object on a finished run and null on one still
    processing. Collapsing that to ``"mixed"`` would throw away both halves, and
    null-versus-populated is precisely the distinction the gateway's own model
    comment records as having dropped 83 matching runs from a production query
    the day it was fudged. A shape that cannot express "object or null" teaches
    P13 to write a required field.
    """
    if token == "null":
        return {**container, "nullable": True}
    if token == "absent":
        return {**container, "optional": True}
    return "mixed"


def body_shape(
    value: object,
    *,
    max_depth: int = MAX_SHAPE_DEPTH,
    max_keys: int = MAX_SHAPE_KEYS,
) -> object:
    """A value-free description of ``value``: type names and key names only.

    Deliberately carries no lengths and no counts. A row count is a fact about
    a real workspace, and a fixture that records one has recorded production
    data however carefully the strings were scrubbed. The only thing an empty
    array contributes is that it *was* empty, which the ``"empty"`` marker says
    without saying how empty anything else was.
    """
    if max_depth <= 0:
        return "truncated"
    if isinstance(value, dict):
        keys = sorted(str(key) for key in value)[:max_keys]
        return {
            "object": {
                key: body_shape(value[key], max_depth=max_depth - 1, max_keys=max_keys)
                for key in keys
            }
        }
    if isinstance(value, list | tuple):
        items: Iterable[object] = list(value)[:MAX_SHAPE_SAMPLE]
        merged: object | None = None
        for item in items:
            shape = body_shape(item, max_depth=max_depth - 1, max_keys=max_keys)
            merged = shape if merged is None else _merge(merged, shape)
        return {"array": merged if merged is not None else "empty"}
    return _scalar_name(value)


__all__ = [
    "ALLOWED_RESPONSE_HEADERS",
    "MARKER",
    "MAX_DETAIL_CHARS",
    "MAX_SHAPE_DEPTH",
    "REQUEST_ID_HEADERS",
    "allowed_headers",
    "body_shape",
    "detail_of",
    "request_id_from",
    "sanitize_detail",
    "scrub_credential",
]
