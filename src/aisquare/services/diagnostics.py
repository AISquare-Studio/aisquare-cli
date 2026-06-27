"""Health checks and introspection."""

from __future__ import annotations

from aisquare.core.injection import load_last
from aisquare.core.stubs import stub
from aisquare.models import InjectionRecord


def status() -> None:
    """Summarise installation health, pools, agents and sync state."""
    stub("status")


def doctor() -> None:
    """Run deep diagnostics and suggest fixes."""
    stub("doctor")


def last_injection() -> InjectionRecord | None:
    """Return the most recent injection record (backs ``why``), or None."""
    return load_last()


def show_log() -> None:
    """Show recent capture and injection activity."""
    stub("log")


def open_home() -> None:
    """Open the aisquare home directory or web dashboard."""
    stub("open")
