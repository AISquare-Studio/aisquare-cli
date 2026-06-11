"""Authentication against the aisquare cloud."""

from __future__ import annotations

from aisquare.core.stubs import stub


def login() -> None:
    """Interactively authenticate and store credentials."""
    stub("login")


def logout() -> None:
    """Discard stored credentials."""
    stub("logout")


def whoami() -> None:
    """Show the identity behind the stored credentials."""
    stub("whoami")


def status() -> None:
    """Report whether credentials exist and are still valid."""
    stub("auth status")


def rotate() -> None:
    """Rotate the stored API token."""
    stub("auth rotate")


def token() -> None:
    """Print the active API token."""
    stub("auth token")
