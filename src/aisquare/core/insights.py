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
"""

from __future__ import annotations

from datetime import UTC, datetime
from functools import lru_cache

from aisquare.core import outbox
from aisquare.core.config import ExplainabilitySettings, load_config

#: Records carry a schema version so a sweeper from a different release can
#: recognise a spool it does not understand instead of mis-shipping it.
RECORD_VERSION = 1

#: Longest text we spool per record. A pasted stack trace or a whole file in a
#: prompt is not an insight, it is a payload — and the gateway charges for it.
#: Truncation is marked so nobody reads a cut span as the whole story.
_MAX_TEXT = 8000
_TRUNCATION_MARK = "… [truncated by aisquare-cli]"


@lru_cache(maxsize=1)
def settings() -> ExplainabilitySettings:
    """Explainability config, read once per process.

    Cached because this is consulted on every prompt and every board write, and
    a CLI process lives for one command — there is no window in which the file
    can change under us that matters.
    """
    try:
        return load_config().explainability
    except Exception:  # a broken config must not break a prompt
        return ExplainabilitySettings()


def shipping_enabled() -> bool:
    """Whether this machine has opted in to sending insights to the gateway."""
    return settings().ship


def reset_cache() -> None:
    """Forget the cached settings (tests, and ``init`` right after it writes)."""
    settings.cache_clear()


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
            "text": _clip(text),
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


def _clip(text: str) -> str:
    if len(text) <= _MAX_TEXT:
        return text
    return text[: _MAX_TEXT - len(_TRUNCATION_MARK)] + _TRUNCATION_MARK
