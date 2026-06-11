"""Organisation policies and their local enforcement."""

from __future__ import annotations

from aisquare.core.stubs import stub


def list_policies() -> None:
    """List policies that apply to this machine."""
    stub("policy list", tier="v1")


def enforcement_status() -> None:
    """Show whether policy enforcement is active."""
    stub("enforce status", tier="v1")


def enable_enforcement() -> None:
    """Enable policy enforcement on this machine."""
    stub("enforce enable", tier="v1")


def disable_enforcement() -> None:
    """Disable policy enforcement on this machine."""
    stub("enforce disable", tier="v1")
