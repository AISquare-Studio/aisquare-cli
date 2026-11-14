"""A role's cycle as the tests of its wording read it (``core.harness.role_cycle``)."""

from __future__ import annotations

MANAGER_MERGE_RULE = (
    "Never write code. Merge a PR only when a person approved it on GitHub and every CI check "
    "on its head is green. Never push."
)
"""The manager's rules on code, merging and pushing, word for word as its cycle states them
once, in both of its variants (the owner's ruling of 2026-11-11, PR #499 redone in #113)."""


def unscoped(verb: str) -> str:
    """A cycle's fleet command as it read before it named the project: what the tests of a
    cycle's wording compare. The board passes ``services.project.fleet_command`` for its
    project, and tests/test_harness.py checks that the manager's then name it."""
    return f"aisquare fleet {verb}"
