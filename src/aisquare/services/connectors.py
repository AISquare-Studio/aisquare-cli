"""Connectors that feed external sources (docs, tickets, ...) into context."""

from __future__ import annotations

from aisquare.core.stubs import stub


def list_connectors() -> None:
    """List available and configured connectors."""
    stub("connectors list", tier="v1")


def add(name: str) -> None:
    """Add and configure a connector."""
    stub("connectors add", tier="v1")


def remove(name: str) -> None:
    """Remove a configured connector."""
    stub("connectors remove", tier="v1")


def status() -> None:
    """Show connector health and last sync times."""
    stub("connectors status", tier="v1")
