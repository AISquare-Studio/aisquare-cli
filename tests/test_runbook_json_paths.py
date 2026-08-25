"""The cutover runbook's scripted checks must resolve against the real payload.

The runbook tells an operator to pipe ``aisquare --json explainability status``
into ``jq``, and separately enumerates the payload's top-level keys so "the
cutover can be scripted rather than eyeballed". Both claims carry a
``[verified-train]`` label. Neither was machine-checked.

One of them was wrong. ``jq -r '.shipping, .spool'`` — the drift-watch command
the runbook itself calls the saving grace against a silently un-drained spool —
printed the bare word ``null`` for its second value, because there is no
top-level ``spool`` key and never was. The counters live one level down, at
``.shipping.queued``/``.sent``/``.dead``, which is also exactly what the
human-readable ``spool:`` line renders. So the data was never missing; the
documented path was.

That failure mode is specific to ``jq -r``: a missing key prints ``null`` on its
own line and exits 0, which in a cron looks like output rather than like a
mistake. An operator watching for a rising backlog would have watched ``null``.

This guard is the reason to keep the fix from coming back. It is deliberately
mechanical in both directions:

- every jq path the runbook pipes the payload into must exist in the payload
- the set of keys the runbook enumerates must be exactly the payload's keys

The extractors assert on their own yield before asserting on the payload — an
extractor that quietly matches nothing would turn this whole file green while
checking nothing, which is the failure mode a documentation guard is most prone
to.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app

RUNBOOK = (
    Path(__file__).resolve().parents[1] / "docs" / "runbooks" / "explainability-prod-cutover.md"
)

# The command the runbook hands the operator. Paths are read out of pipelines
# built on exactly this, so a different subcommand's payload is never conflated.
STATUS_COMMAND = "--json explainability status"

# Anchors the payload enumeration. Both copies (§5 and the findings list) open
# with this phrase and then list the keys as backticked identifiers.
ENUMERATION_ANCHOR = "returns a real payload"


@pytest.fixture(scope="module")
def runbook() -> str:
    if not RUNBOOK.exists():  # pragma: no cover - the path is the point of the test
        pytest.fail(f"the runbook this guard exists for is missing: {RUNBOOK}")
    return RUNBOOK.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def payload() -> dict[str, object]:
    """The real payload, from the CLI, on a machine with nothing configured.

    Cold is the right state to assert against: the runbook's reader runs these
    commands *while* configuring, so a key that only appears once shipping is on
    is not a key they can script against.
    """
    result = CliRunner().invoke(app, ["--json", "explainability", "status"])
    assert result.exit_code == 0, result.output
    parsed: dict[str, object] = json.loads(result.output)
    return parsed


def _jq_paths(text: str) -> list[str]:
    """Dotted paths piped out of the status payload, e.g. ``.shipping.gateway``.

    Handles both quoted (``jq -r '.a, .b'``) and bare (``jq -r .a.b``) forms,
    which the runbook uses in different places.
    """
    paths: list[str] = []
    for line in text.splitlines():
        stripped = line.lstrip("> ").strip()
        if STATUS_COMMAND not in stripped or "jq" not in stripped:
            continue
        expression = stripped.split("jq", 1)[1]
        paths.extend(re.findall(r"\.[A-Za-z_][A-Za-z0-9_.]*", expression))
    return paths


def _enumerated_keys(text: str) -> set[str]:
    """Key names the runbook claims the payload has.

    Parentheticals are removed first — the list reads
    ``key_env``/``key_set`` (never the key itself), ``proxy``, … — so the aside
    would otherwise cut the run of backticked names in half.

    The slash group repeats (``*``, not ``?``). It allowed exactly two names,
    which was every group the doc had until the key fields became four —
    ``key_env``/``key_set``/``key_source``/``key_origin`` — and then the run
    stopped at the third slash and the enumeration silently lost everything
    after it. The failure was legible (this file's sibling assertion reported
    eight missing keys) but it accused the DOC of omitting names it listed, so
    the extractor is the thing that was wrong.
    """
    keys: set[str] = set()
    for match in re.finditer(re.escape(ENUMERATION_ANCHOR), text):
        window = text[match.end() : match.end() + 600]
        window = " ".join(line.lstrip("> ").strip() for line in window.splitlines())
        window = re.sub(r"\([^)]*\)", "", window)
        run = re.search(r"(?:`[a-z_]+`(?:\s*/\s*`[a-z_]+`)*(?:\s*,\s*)?)+", window)
        assert run, f"the payload enumeration stopped parsing near: {window[:120]!r}"
        keys.update(re.findall(r"`([a-z_]+)`", run.group(0)))
    return keys


def _resolves(payload: object, path: str) -> bool:
    cursor = payload
    for segment in path.strip(".").split("."):
        if not isinstance(cursor, dict) or segment not in cursor:
            return False
        cursor = cursor[segment]
    return True


def test_the_extractors_actually_found_something(runbook: str) -> None:
    """Guard the guard: a silent no-match would make every assertion vacuous."""
    paths = _jq_paths(runbook)
    keys = _enumerated_keys(runbook)

    assert len(paths) >= 2, f"expected several documented jq paths, found {paths}"
    assert len(keys) >= 8, f"expected the full payload enumeration, found {sorted(keys)}"
    assert ENUMERATION_ANCHOR in runbook


def test_every_documented_jq_path_resolves(runbook: str, payload: dict[str, object]) -> None:
    """The bug this file was written for.

    ``jq -r`` on a missing key prints ``null`` and exits 0 — a scripted check
    reading it cannot tell that apart from a real value.
    """
    missing = sorted({path for path in _jq_paths(runbook) if not _resolves(payload, path)})

    assert not missing, (
        f"the runbook pipes the status payload into {missing}, which the payload "
        f"does not have — jq prints 'null' and exits 0. Payload keys: "
        f"{sorted(payload)}"
    )


def test_the_enumerated_keys_are_exactly_the_payload_keys(
    runbook: str, payload: dict[str, object]
) -> None:
    """Drift in either direction is a defect, so assert equality, not inclusion.

    A key the runbook promises but the payload lacks breaks a scripted cutover.
    A key the payload gained but the runbook never mentions is a surface an
    operator will not know to read.
    """
    enumerated = _enumerated_keys(runbook)
    actual = set(payload)

    assert enumerated == actual, (
        f"runbook promises but payload lacks: {sorted(enumerated - actual)}; "
        f"payload has but runbook omits: {sorted(actual - enumerated)}"
    )


def test_the_spool_counters_are_reachable_where_the_runbook_now_points(
    payload: dict[str, object],
) -> None:
    """The replacement path must carry the data the removed one implied.

    Deleting ``.spool`` from the runbook is only correct because the same three
    numbers are already under ``.shipping``. If they ever move, this fails and
    the runbook needs rewording rather than the reader guessing.
    """
    for counter in ("queued", "sent", "dead"):
        assert _resolves(payload, f".shipping.{counter}"), (
            f".shipping.{counter} is gone — the runbook's drift-watch command "
            "no longer shows a backlog, and nothing else in the JSON does"
        )
