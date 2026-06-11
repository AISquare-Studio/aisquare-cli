"""Background capture of agent activity into the context pools."""

from __future__ import annotations

from aisquare.core.stubs import stub


def status() -> None:
    """Report whether capture is running and what it last saw."""
    stub("capture status", tier="v1")


def pause() -> None:
    """Temporarily pause capture without removing hooks."""
    stub("capture pause", tier="v1")


def resume() -> None:
    """Resume a paused capture pipeline."""
    stub("capture resume", tier="v1")


def start() -> None:
    """Start the capture pipeline."""
    stub("capture start", tier="v1")


def stop() -> None:
    """Stop the capture pipeline."""
    stub("capture stop", tier="v1")
