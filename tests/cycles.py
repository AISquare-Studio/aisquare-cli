"""A role's cycle as the tests of its wording read it (``core.harness.role_cycle``)."""

from __future__ import annotations

MANAGER_MERGE_RULE = (
    "Merge a PR only when a person approved it on GitHub and every CI check on its head is "
    "green. Never push."
)
"""The manager's merge rule, word for word as its cycle states it."""


def unscoped(verb: str) -> str:
    """A cycle's fleet command as it read before it named the project: what the tests of a
    cycle's wording compare. The board passes ``services.project.fleet_command`` for its
    project, and tests/test_harness.py checks that the manager's then name it."""
    return f"aisquare fleet {verb}"
