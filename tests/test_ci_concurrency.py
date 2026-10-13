"""A scheduled run of a workflow never shares a concurrency group with a push.

Every workflow here cancels the run its group already has (``cancel-in-progress``),
which is what drops a push superseded by the next one. Grouped by ref alone, the
nightly on main and a push to main were one group, so either one cancelled the other
(follow-up of #257). The group is worked out here as GitHub works it out, for each
workflow with a ``schedule`` trigger.

Read as text, as ``test_ci_covers_the_ambient_environment.py`` reads ``ci.yml``:
``pyyaml`` is not a dependency of this project. A group this cannot work out (any
expression but a ``github.<name>`` lookup, or a name it is not given) fails the test
rather than passing it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"

#: The workflow-level ``concurrency`` block's ``group``: unindented, so no job's.
_GROUP = re.compile(r"^concurrency:\n(?:  .*\n)*?  group: (.+)$", re.MULTILINE)
_NAME = re.compile(r"^name: (.+)$", re.MULTILINE)
_SCHEDULE = re.compile(r"^  schedule:$", re.MULTILINE)
_EXPRESSION = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")
_LOOKUP = re.compile(r"github\.([a-z_]+)")


def _scheduled() -> list[Path]:
    """The workflows with a ``schedule`` trigger."""
    return sorted(
        path
        for path in WORKFLOWS.glob("*.yml")
        if _SCHEDULE.search(path.read_text(encoding="utf-8"))
    )


def _group(workflow: Path, **github: str) -> str:
    """``workflow``'s concurrency group for a run whose ``github`` context is ``github``."""
    text = workflow.read_text(encoding="utf-8")
    found, name = _GROUP.search(text), _NAME.search(text)
    assert found is not None, f"{workflow.name}: no workflow-level concurrency group"
    assert name is not None, f"{workflow.name}: no name"
    context = {"workflow": name.group(1).strip(), **github}

    def value(expression: re.Match[str]) -> str:
        lookup = _LOOKUP.fullmatch(expression.group(1))
        assert lookup is not None, f"{workflow.name}: cannot work out {expression.group(0)}"
        assert lookup.group(1) in context, f"{workflow.name}: no github.{lookup.group(1)} here"
        return context[lookup.group(1)]

    return _EXPRESSION.sub(value, found.group(1).strip())


def test_the_scheduled_workflows_are_the_ones_known_to_run_nightly() -> None:
    """What the test below must see: a rule over no workflow passes for any group."""
    assert {path.name for path in _scheduled()} >= {"ci.yml", "install.yml"}


@pytest.mark.parametrize("workflow", _scheduled(), ids=lambda path: path.name)
def test_a_scheduled_run_and_a_push_to_main_are_never_one_group(workflow: Path) -> None:
    main = "refs/heads/main"
    nightly = _group(workflow, event_name="schedule", ref=main, sha="a" * 40, run_id="1")
    push = _group(workflow, event_name="push", ref=main, sha="b" * 40, run_id="2")
    next_push = _group(workflow, event_name="push", ref=main, sha="c" * 40, run_id="3")
    by_hand = _group(workflow, event_name="workflow_dispatch", ref=main, sha="c" * 40, run_id="4")

    assert nightly != push, f"{workflow.name}: the nightly and a push to main share {push!r}"
    assert by_hand != push, f"{workflow.name}: a run by hand and a push share {push!r}"
    assert push == next_push, f"{workflow.name}: a superseded push is no longer cancelled"
