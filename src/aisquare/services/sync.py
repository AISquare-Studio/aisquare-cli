"""Synchronisation of local context with the aisquare cloud."""

from __future__ import annotations

from aisquare.core.stubs import stub


def sync() -> None:
    """Push and pull context between this machine and the cloud."""
    stub("sync", tier="v1")
