"""``aisquare capture`` — the background capture pipeline."""

from __future__ import annotations

import typer

from aisquare.services import capture as capture_service

app = typer.Typer(help="Control background capture of agent activity.", no_args_is_help=True)


@app.command("status")
def status() -> None:
    """Show whether capture is running."""
    capture_service.status()


@app.command("pause")
def pause() -> None:
    """Pause capture without removing hooks."""
    capture_service.pause()


@app.command("resume")
def resume() -> None:
    """Resume paused capture."""
    capture_service.resume()


@app.command("start")
def start() -> None:
    """Start the capture pipeline."""
    capture_service.start()


@app.command("stop")
def stop() -> None:
    """Stop the capture pipeline."""
    capture_service.stop()
