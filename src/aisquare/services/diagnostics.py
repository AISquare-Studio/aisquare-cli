"""Health checks and introspection."""

from __future__ import annotations

from aisquare.core.stubs import stub


def status() -> None:
    """Summarise installation health, pools, agents and sync state."""
    stub("status")


def doctor() -> None:
    """Run deep diagnostics and suggest fixes."""
    stub("doctor")


def why() -> None:
    """Explain the most recent context injection."""
    stub("why")


def show_log() -> None:
    """Show recent capture and injection activity."""
    stub("log")


def open_home() -> None:
    """Open the aisquare home directory or web dashboard."""
    stub("open")
