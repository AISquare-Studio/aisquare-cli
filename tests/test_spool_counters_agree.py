"""The JSON has no ``spool`` key, on purpose — and this is why that is safe.

``explainability status`` prints the spool counts on their own line; the JSON
carries the same three numbers one level down, at ``.shipping.queued/.sent/
.dead``. The runbook promised a top-level ``.spool`` that never existed, which
``jq -r`` answered with the bare word ``null`` and exit 0 — a drift-watch that
could not see drift. That was fixed on the page rather than in the payload,
and the whole justification for fixing it there is one sentence: *the same
three numbers are already under ``.shipping``*.

That sentence had only ever been witnessed on a cold machine, where all three
counters are 0 and ``0 == 0`` holds even if the two surfaces read different
sources. So the equality is pinned here with a REAL backlog and three DISTINCT
values, which also catches a transposition — 1/2/3 read as 3/2/1 is a bug that
matching-shaped assertions on zeros can never see.

The decision itself was not undocumented, but it was recorded in
``test_redaction_surface.py``'s ``restructured = {"key", "spool"}`` — a place
the payload's author has no reason to open. It now also sits at the
construction site.
"""

from __future__ import annotations

import json
import re

from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import insights, outbox
from aisquare.core.config import AppConfig, save_config

#: The counters, wherever they sit on the line. The ``$`` anchor used to end at
#: ``dead-letter``, and the line now carries the queue directory after them —
#: which broke this guard without touching what it asserts. The suffix is
#: deliberately tolerated and the COUNTER PHRASE is not: the three numbers, in
#: that order, with those words, still have to be there for a match.
_SPOOL_LINE = re.compile(
    r"^spool:\s+(\d+) queued, (\d+) sent, (\d+) dead-letter(?: .*)?$", re.MULTILINE
)


def _configure_shipping() -> None:
    config = AppConfig()
    config.explainability.ship = True
    config.explainability.gateway_url = "https://gateway.invalid"
    save_config(config)
    insights.reset_cache()


def _backlog_of(queued: int, sent: int, dead: int) -> None:
    """A spool holding three DIFFERENT counts, so order is observable."""
    for index in range(queued + sent + dead):
        outbox.enqueue({"kind": "prompt", "text": f"p{index}"})
    records = outbox.pending()
    for path in records[:sent]:
        claimed = outbox.claim(path)
        assert claimed is not None
        outbox.mark_sent(claimed)
    for path in records[sent : sent + dead]:
        claimed = outbox.claim(path)
        assert claimed is not None
        outbox.mark_dead(claimed, "409 no_agent_identity")


def test_the_human_line_and_the_json_report_the_same_counters(runner: CliRunner) -> None:
    """One source, two renderings — asserted where the numbers can disagree.

    If the JSON is ever built from a second call to the spool, this is the test
    that notices: today both surfaces render one ``shipping_state`` object, and
    that is precisely the property the runbook fix depends on.
    """
    _configure_shipping()
    _backlog_of(queued=1, sent=2, dead=3)

    human = runner.invoke(app, ["explainability", "status"], catch_exceptions=False).output
    payload = json.loads(
        runner.invoke(app, ["--json", "explainability", "status"], catch_exceptions=False).output
    )

    match = _SPOOL_LINE.search(human)
    assert match, f"the human view no longer prints a spool: line\n{human}"
    rendered = tuple(int(value) for value in match.groups())

    assert rendered == (1, 2, 3), "the fixture itself is wrong if this fails"
    shipping = payload["shipping"]
    assert (shipping["queued"], shipping["sent"], shipping["dead"]) == rendered, (
        "the spool: line and .shipping disagree — the runbook tells operators to "
        "watch .shipping.queued for a backlog the human view would show them"
    )


def test_the_payload_carries_no_top_level_spool_key(runner: CliRunner) -> None:
    """A decision, pinned so that reversing it has to be deliberate.

    Adding ``spool`` would put the same three integers on two paths in one
    document, and a payload with two answers has no canonical one. If a future
    change wants it anyway, that is a real choice and not a typo: add the key,
    list it in the runbook's key enumeration, and ``test_runbook_json_paths``
    (which asserts the enumeration EQUALS the payload's keys) will hold the two
    together.
    """
    _configure_shipping()
    _backlog_of(queued=1, sent=2, dead=3)

    payload = json.loads(
        runner.invoke(app, ["--json", "explainability", "status"], catch_exceptions=False).output
    )

    assert "spool" not in payload, (
        "a top-level 'spool' key appeared — if that is deliberate, update the "
        "runbook's key enumeration too, or the page and the payload drift apart "
        "again in the direction that reads as data"
    )
    assert {"queued", "sent", "dead"} <= payload["shipping"].keys()
