"""Detection and integration of coding agents (Claude Code, etc.)."""

from __future__ import annotations

from aisquare.core.stubs import stub
from aisquare.models import AgentInfo


def list_agents() -> list[AgentInfo]:
    """List agents aisquare knows about and their connection state."""
    stub("agents list")


def connect(name: str) -> None:
    """Install the aisquare hook into the named agent."""
    stub("agents connect")


def disconnect(name: str) -> None:
    """Remove the aisquare hook from the named agent."""
    stub("agents disconnect")


def scan() -> list[AgentInfo]:
    """Scan this machine for installed agents."""
    stub("agents scan")


def status(name: str | None = None) -> None:
    """Show integration health for one agent, or all of them."""
    stub("agents status")
