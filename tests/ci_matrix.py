"""``.github/workflows/ci.yml`` read as GitHub reads it, for the tests that pin its shape.

A matrix there is chosen with ``fromJSON(<lean> && '<a>' || '<b>')``: the JSON strings
are what GitHub parses, so evaluating the choice for both events and expanding each
matrix as GitHub does pins exactly which jobs a run starts.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent

LEAN = "github.event_name == 'pull_request' && startsWith(github.base_ref, 'rc/')"
"""The lean test, spelled as every job in ci.yml spells it (a sub-PR into ``rc/**``)."""

_CHOICE = re.compile(
    r"^\$\{\{\s*fromJSON\(\s*(?P<cond>.+?)\s*&&\s*'(?P<lean>[^']*)'\s*\|\|\s*'(?P<full>[^']*)'\s*\)\s*\}\}$",
    re.DOTALL,
)


def workflow(name: str = "ci.yml") -> dict[Any, Any]:
    loaded: dict[Any, Any] = yaml.safe_load(
        (ROOT / ".github/workflows" / name).read_text(encoding="utf-8")
    )
    return loaded


def choose(value: Any, *, lean: bool) -> Any:
    """``value`` as GitHub resolves it for a sub-PR (``lean``) or any other event."""
    match = _CHOICE.match(" ".join(str(value).split()))
    assert match is not None, f"not a lean/full choice: {value!r}"
    cond = " ".join(match["cond"].split())
    assert cond == f"({LEAN})", f"the lean test is spelled differently here: {cond}"
    return json.loads(match["lean" if lean else "full"])


def expand(matrix: dict[str, Any]) -> list[dict[str, Any]]:
    """The jobs a matrix starts, by GitHub's rules.

    The cross-product of its vectors, less every combination an ``exclude`` entry
    matches (a partial match is enough). Then each ``include`` entry is added to every
    combination of the cross-product whose ORIGINAL values it does not overwrite (values
    an earlier entry added may be); one it can add to none of them is a job of its own.
    """
    axes = {key: values for key, values in matrix.items() if key not in ("include", "exclude")}
    combos: list[dict[str, Any]] = [{}] if axes else []
    for key, values in axes.items():
        combos = [{**combo, key: value} for combo in combos for value in values]
    combos = [
        combo
        for combo in combos
        if not any(
            all(combo.get(k) == v for k, v in rule.items()) for rule in matrix.get("exclude", [])
        )
    ]
    jobs = [dict(combo) for combo in combos]
    extra: list[dict[str, Any]] = []
    for entry in matrix.get("include", []):
        fits = [
            i
            for i, combo in enumerate(combos)
            if all(combo[k] == v for k, v in entry.items() if k in combo)
        ]
        for i in fits:
            jobs[i].update(entry)
        if not fits:
            extra.append(dict(entry))
    return jobs + extra


def jobs(name: str, *, lean: bool) -> list[dict[str, Any]]:
    """The matrix jobs of ci.yml's job ``name`` for a sub-PR (``lean``) or any other run."""
    return expand(choose(workflow()["jobs"][name]["strategy"]["matrix"], lean=lean))
