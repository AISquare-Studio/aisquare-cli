"""#293 — a fleet across machines, the interim way: a manager with SSH to another box.

The feature is postponed; the runbook for the workaround and the design sketch ship
instead. This pins that the runbook exists, that its commands are checked against the
CLI like every other runbook's, that it says what B gives A (the trust model), and that
`docs/fleet.md`'s "Later" list points at it.
"""

from __future__ import annotations

from pathlib import Path

from tests import test_documented_commands as documented

REPO = Path(__file__).resolve().parents[1]
RUNBOOK = "docs/runbooks/multi-machine-ssh.md"
SKETCH = "docs/plans/multi-machine-fleet.md"


def test_the_runbook_and_the_sketch_exist_and_are_linked() -> None:
    text = (REPO / RUNBOOK).read_text(encoding="utf-8")
    for must in ("ControlMaster", "ServerAliveInterval", "command=", "restrict", "postponed"):
        assert must in text, must
    sketch = (REPO / SKETCH).read_text(encoding="utf-8")
    assert "postponed" in sketch.lower() and "--host" in sketch
    later = (REPO / "docs/fleet.md").read_text(encoding="utf-8").split("## Later", 1)[1]
    assert "runbooks/multi-machine-ssh.md" in later


def test_its_commands_are_checked_against_the_cli() -> None:
    assert RUNBOOK in documented.DOCUMENTED
