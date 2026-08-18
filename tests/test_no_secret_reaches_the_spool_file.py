"""Redaction is asserted on one FIELD; the property is about the BYTES on disk.

``test_shipping_redaction.py`` is thorough about SHAPES — eleven credential
formats, standard and strict modes, prose left alone. Every on-disk assertion
in it goes through one helper::

    def _spooled_text() -> str:
        return ... json.loads(p.read_text()).get("text", "") ...

It reads the ``text`` field. A spooled record carries TWELVE: ``at``,
``event_id``, ``event_kind``, ``kind``, ``project_id``, ``role``, ``run_key``,
``seq``, ``session_id``, ``task_id``, ``text``, ``v``. A secret landing in any
of the other eleven satisfies every existing assertion. The handoff's claim is
"scrubbed INTO the spool"; the tests say "scrubbed out of one field of it".
The observation channel is narrower than the property — the same defect this
shift has now found from four directions.

MEASURED AT 2dc9560 BEFORE WRITING ANY OF THIS, across three capture paths
including the prompt hook: six spooled records, ZERO leaks in the bytes. The
behaviour is already correct at the full width. This pins it there, because
this is the one property whose regression is unrecoverable — bytes on disk get
shipped to a gateway, and "we scrubbed the text field" is no defence if a later
change adds a field carrying raw input.

THE PROMPT HOOK IS COVERED HERE AND NOWHERE ELSE. It captures every prompt of
every session, which makes it the widest free-text surface in the product and
the likeliest place a person pastes a key while asking for help with it.

Values are assembled from obviously synthetic parts by the module this borrows
them from, so nothing here is credential-shaped to a scanner.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import insights, outbox
from aisquare.core.config import AppConfig, save_config
from aisquare.models import RedactionLevel

from .test_shipping_redaction import SECRETS


def _configure(level: RedactionLevel = RedactionLevel.standard) -> None:
    config = AppConfig()
    config.explainability.ship = True
    config.explainability.gateway_url = "https://gateway.example"
    config.redaction.level = level
    save_config(config)
    insights.reset_cache()


def _spooled_bytes() -> str:
    """Every byte of every pending record, unparsed.

    Unparsed on purpose: parsing is what narrowed the original assertion to a
    single field. A secret in a key, in a nested structure, or in a field added
    next year is still in this string.
    """
    return "\n".join(path.read_text(encoding="utf-8") for path in outbox.pending())


def _capture_via_note(runner: CliRunner, text: str) -> None:
    runner.invoke(app, ["note", text], catch_exceptions=True)


def _capture_via_task(runner: CliRunner, text: str) -> None:
    runner.invoke(app, ["task", "add", text], catch_exceptions=True)


def _capture_via_prompt(runner: CliRunner, text: str) -> None:
    payload = json.dumps({"session_id": "redaction-probe", "cwd": os.getcwd(), "prompt": text})
    runner.invoke(app, ["hook", "user-prompt-submit"], input=payload, catch_exceptions=True)


_PATHS = {
    "note": _capture_via_note,
    "task": _capture_via_task,
    "prompt hook": _capture_via_prompt,
}


@pytest.mark.parametrize("path", sorted(_PATHS))
@pytest.mark.parametrize("label", sorted(SECRETS))
def test_no_secret_appears_anywhere_in_the_spooled_bytes(
    runner: CliRunner, isolated_home: Path, path: str, label: str
) -> None:
    """The property at its real width, across every path that reaches the spool."""
    runner.invoke(app, ["init", "--yes"], catch_exceptions=True)
    _configure()
    secret = SECRETS[label]

    _PATHS[path](runner, f"deploy is broken, {secret} was rejected, please help")

    assert secret not in _spooled_bytes(), (
        f"the {label} pasted through {path} reached the spool file verbatim"
    )


@pytest.mark.parametrize("path", sorted(_PATHS))
def test_the_harness_can_see_a_leak(runner: CliRunner, isolated_home: Path, path: str) -> None:
    """The control, and without it every assertion above is unfalsifiable.

    With redaction OFF the same secret through the same path MUST appear in the
    bytes. If it does not, the capture produced nothing and "no leak found" was
    never about redaction at all — which is exactly how a probe that spools
    nothing reads as a clean result.
    """
    runner.invoke(app, ["init", "--yes"], catch_exceptions=True)
    _configure(RedactionLevel.off)
    secret = SECRETS["github pat"]

    _PATHS[path](runner, f"deploy is broken, {secret} was rejected, please help")

    assert secret in _spooled_bytes(), (
        f"redaction OFF and nothing leaked through {path} — this path spools "
        "nothing, so the redaction assertions about it prove nothing either"
    )


def test_the_bytes_view_is_wider_than_the_field_view() -> None:
    """Why this file exists, demonstrated rather than asserted in prose.

    The gap is a property of the ASSERTION, not of the writer: a helper that
    parses a record and reads one key cannot see a secret under another key,
    whatever the writer does. Shown here on a synthetic record so the
    demonstration does not depend on production code being broken.

    HOW FAR THAT REACHES, HONESTLY. My first attempt to demonstrate it by
    smuggling a field into the real writer FAILED — the record shape is
    validated, the spool produced nothing, and both this suite and the field-
    based one failed for that unrelated reason. So the leak is not one stray
    dict key away: it takes a model change. The gap is real and the road to it
    is longer than "any of the other eleven fields", which is what I first
    wrote.
    """
    secret = SECRETS["github pat"]
    record = {
        "v": 1,
        "kind": "note",
        "text": "deploy is broken, [redacted] was rejected",
        "a_field_added_later": f"deploy is broken, {secret} was rejected",
    }

    field_view = str(record.get("text", ""))
    bytes_view = json.dumps(record)

    assert secret not in field_view, "the synthetic record is not demonstrating the gap"
    assert secret in bytes_view, "the synthetic record is not demonstrating the gap"


def test_the_real_record_still_has_more_than_one_field(
    runner: CliRunner, isolated_home: Path
) -> None:
    """If the record ever collapses to just ``text``, retire this file.

    An unexamined guard is worse than none, and this one's whole justification
    is that there is more on disk than the field the other suite reads.
    """
    runner.invoke(app, ["init", "--yes"], catch_exceptions=True)
    _configure()

    _capture_via_note(runner, "an ordinary note with no credential in it")

    pending = outbox.pending()
    assert pending, "fixture premise: something must be spooled"
    record = json.loads(pending[0].read_text(encoding="utf-8"))
    assert len(record) > 1, "the record now has one field; this file has no gap to guard"
    assert "text" in record, "the field the other suite checks has been renamed"
