"""What the CLI captures for the explainability gateway, and when it captures it.

Two seams feed this module — ``services.hooks.capture_prompt`` (every human
prompt) and ``services.team._emit`` (every board event: notes, task claims,
dones, decisions, session lifecycle). Both are on the primary path, so
everything here is deliberately cheap: one cached config read and one small
local file write, no network, no store queries, no imports beyond the stdlib.
Identity resolution and delivery happen later, in the sweeper process.

The hot-path predicate is a single boolean, ``explainability.ship``. It is the
user's explicit opt-in and it is written only once both a gateway URL and a
usable key exist (see ``services.explainability.configure_shipping``), which is
what makes "no key or config ⇒ nothing captured, nothing logged as error" a
property of the design rather than a check anyone has to remember.

Text is scrubbed HERE, on its way into the spool, rather than in the sweeper
that sends it — see :mod:`aisquare.core.redaction`. A prompt containing a
pasted key must never be written to disk in a file whose entire purpose is to
be uploaded, not even for the seconds before the next drain. What the user
typed is still recorded verbatim by the LOCAL capture that runs before this;
only the outbound copy is scrubbed.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from functools import lru_cache

from aisquare.core import outbox
from aisquare.core.config import AppConfig, ExplainabilitySettings, load_config
from aisquare.core.redaction import redact
from aisquare.models import RedactionLevel

#: Records carry a schema version so a sweeper from a different release can
#: recognise a spool it does not understand instead of mis-shipping it.
RECORD_VERSION = 2

#: The Run this process's model traffic is already going to, exported by the
#: launcher alongside the proxy headers. It is the ``X-Pipeline-Id`` the proxy
#: sends, so keying our spans on it is what puts the two lanes in ONE Run.
#:
#: Usually it equals the board session id and the distinction is invisible. It
#: does not when the launch could not be joined — a wrapper binary, ``--resume``,
#: ``--continue`` — where the launcher mints a pipeline id it cannot pin to the
#: agent's own session id. Keying on the board id there would file our insights
#: in a Run of their own while the model traffic went somewhere else: two Runs
#: for one session, which is the fragmentation the doctrine forbids. The board
#: ids still travel inside each record, so a span joins back to its row either
#: way.
#:
#: The name is duplicated from ``services.explainability.PIPELINE_ID_ENV_VAR``
#: rather than imported: that module pulls urllib and the store, and this one
#: runs on the primary path. ``test_the_run_key_env_var_matches_the_launcher``
#: fails if the two ever drift.
RUN_KEY_ENV_VAR = "AISQUARE_PIPELINE_ID"

#: Longest text we spool per record. A pasted stack trace or a whole file in a
#: prompt is not an insight, it is a payload — and the gateway charges for it.
#: Truncation is marked so nobody reads a cut span as the whole story.
_MAX_TEXT = 8000
_TRUNCATION_MARK = "… [truncated by aisquare-cli]"


@lru_cache(maxsize=1)
def _config() -> AppConfig:
    """The whole config, read once per process.

    Cached because it is consulted on every prompt and every board write, and a
    CLI process lives for one command — there is no window in which the file can
    change under us that matters. One read rather than two because the shipping
    decision and the redaction level are consulted together, every time.
    """
    try:
        return load_config()
    except Exception:  # a broken config must not break a prompt
        return AppConfig()


def settings() -> ExplainabilitySettings:
    """Explainability config for this process."""
    return _config().explainability


def redaction_level() -> RedactionLevel:
    """How hard to scrub text before it is spooled for the gateway."""
    return _config().redaction.level


def shipping_enabled() -> bool:
    """Whether this machine has opted in to sending insights to the gateway."""
    return settings().ship


def reset_cache() -> None:
    """Forget the cached settings (tests, and ``init`` right after it writes)."""
    _config.cache_clear()


def run_key(session_id: str | None) -> str | None:
    """The Run these insights belong in — the launcher's answer, then ours.

    Read from the ambient environment rather than passed in, because the
    processes that capture (a hook fire, an ``aisquare note``) are children of
    the traced session and inherit its wiring. Whoever launched them already
    decided which Run their model traffic joins; agreeing with that decision is
    the whole job. See :data:`RUN_KEY_ENV_VAR`.
    """
    return os.environ.get(RUN_KEY_ENV_VAR, "").strip() or session_id


def record_prompt(
    text: str,
    *,
    session_id: str | None = None,
    project_id: str | None = None,
) -> None:
    """Spool one human prompt. No-op unless shipping is configured."""
    if not shipping_enabled():
        return
    _spool(
        kind="prompt",
        text=text,
        session_id=session_id,
        project_id=project_id,
    )


def record_team_event(
    *,
    event_kind: str,
    text: str,
    event_id: str | None = None,
    session_id: str | None = None,
    project_id: str | None = None,
    task_id: str | None = None,
    seq: int | None = None,
    role: str | None = None,
) -> None:
    """Spool one board event. No-op unless shipping is configured.

    ``seq`` is the board receipt number — the same one ``aisquare team verify``
    re-checks — and ``event_id`` is the row's durable id. Both ride along so a
    span at the gateway joins back to the exact ``aisquare note`` that produced
    it, rather than being matched on a timestamp and a prayer.
    """
    if not shipping_enabled():
        return
    _spool(
        kind="team_event",
        text=text,
        event_id=event_id,
        session_id=session_id,
        project_id=project_id,
        event_kind=event_kind,
        task_id=task_id,
        seq=seq,
        role=role,
    )


def _spool(
    *,
    kind: str,
    text: str,
    session_id: str | None,
    project_id: str | None,
    event_id: str | None = None,
    event_kind: str | None = None,
    task_id: str | None = None,
    seq: int | None = None,
    role: str | None = None,
) -> None:
    # The guard is HERE, at the observer's own boundary, not delegated to
    # ``outbox.enqueue``'s. Building the record is the part that touches caller
    # data — a text that is not a str, a clock that raises, an outbox module
    # someone refactors — and the caller is mid-prompt or mid-board-write. A
    # test pins this: patching enqueue to raise must not cost `aisquare note`
    # its exit code.
    try:
        record: dict[str, object] = {
            "v": RECORD_VERSION,
            "kind": kind,
            "at": datetime.now(tz=UTC).isoformat(),
            # _outbound clips AND scrubs; the fix that added run_key predates
            # redaction landing, so this is both halves, not a choice between.
            "text": _outbound(text),
            "run_key": run_key(session_id),
            "event_id": event_id,
            "session_id": session_id,
            "project_id": project_id,
            "event_kind": event_kind,
            "task_id": task_id,
            "seq": seq,
            "role": role,
        }
        outbox.enqueue(record)
    except Exception:  # an observer may not break its subject
        return


def _outbound(text: str) -> str:
    """The text as it will leave the machine: clipped, then scrubbed.

    Clip FIRST so the scrubber's work is bounded by ``_MAX_TEXT`` no matter how
    large a paste was — this runs while a human waits, and an unbounded regex
    sweep over a 5MB paste is a hang. Anything past the cap is dropped outright
    and so cannot leak. The one boundary case is a credential straddling the
    cut: its head survives into the clipped text and is still matched, because
    every vendor pattern needs only its prefix plus ~16 characters, and a
    shorter fragment than that is not a usable credential.
    """
    return redact(_clip(text), redaction_level())


def _clip(text: str) -> str:
    if len(text) <= _MAX_TEXT:
        return text
    return text[: _MAX_TEXT - len(_TRUNCATION_MARK)] + _TRUNCATION_MARK
