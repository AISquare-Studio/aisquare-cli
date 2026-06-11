"""Projects (workspaces): identity, linking and onboarding."""

from __future__ import annotations

from pathlib import Path

from aisquare.core.stubs import stub
from aisquare.models import ProjectInfo


def info() -> ProjectInfo:
    """Describe the active project."""
    stub("project info")


def list_projects() -> list[ProjectInfo]:
    """List all known projects."""
    stub("project list")


def switch(name: str) -> None:
    """Make ``name`` the active project."""
    stub("project switch")


def link(repo: str) -> None:
    """Link a repository into the active project."""
    stub("project link")


def onboard(path: Path | None, *, refresh: bool) -> None:
    """Analyse a project tree and seed its context pool."""
    stub("project onboard")
