"""verdict.json: the run in the QA org's release-gate shape.

    {"verdict", "stage": "e2e", "env", "ref", "counts": {passed, failed, error, skipped},
     "failures": [{"test", "class", "when", "message"}], "preflight_failed", "notes"}

Exactly those keys and no others: the gate validates the file against a schema
we may not see (PLAN.md §9 vendors it later), and a schema that closes its
objects rejects a helpful extra field as surely as a missing one.

Re-implemented from that shape, not copied from the gate's code, which lives
in a private repository.
"""

from __future__ import annotations

from typing import Any

from e2e.report.facts import Facts, env_line

MESSAGE_LIMIT = 1000
"""A failure message is a pointer, not the evidence: the gallery holds the rest."""


def payload(facts: Facts) -> dict[str, Any]:
    return {
        "verdict": facts.verdict,
        "stage": "e2e",
        "env": env_line(facts.run),
        "ref": facts.run.env.commit,
        "counts": facts.counts.as_dict(),
        "failures": [
            {
                "test": failure.test,
                "class": failure.verdict_class,
                "when": failure.when,
                "message": _clip(failure.message),
            }
            for failure in facts.failures
        ],
        "preflight_failed": facts.preflight_failed,
        # Watch items never gate, so they are not failures; their news goes here.
        "notes": [*facts.notes, *facts.watch_notes],
    }


def _clip(text: str) -> str:
    text = " ".join(text.split())  # one line: the gate prints it in a table
    return text if len(text) <= MESSAGE_LIMIT else text[: MESSAGE_LIMIT - 1] + "…"
