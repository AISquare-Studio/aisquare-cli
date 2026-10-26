"""The flags a role's launch gets for its tools and skill listing at the built-in config
(``harness.tool_policy``): for tests whose subject is something else in the argv, so that
each still pins the argv whole."""

from __future__ import annotations

from aisquare.core import harness
from aisquare.core.config import FleetSettings


def policy_args(role: str) -> list[str]:
    """What ``tool_policy`` adds to a Claude Code launch of ``role`` that asks nothing itself."""
    return harness.tool_policy(role, binary="claude", args=[], fleet=FleetSettings()).args
