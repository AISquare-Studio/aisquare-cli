"""The Onboard view: pick a directory, run `init` + `doctor` in the background, list it.

Phase 2 (docs/plans/fleet-tui.md §4.2, §5.6) fills this in. The contract the
shell relies on is fixed here: ``OnboardView`` posts ``ProjectOnboarded`` with
the new project's id when the background work succeeds, and ``OnboardFailed``
with the reason otherwise. Work runs through ``core.selfcli`` with
``cwd=<path>`` — the UI process never changes directory.
"""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Input, Static


class ProjectOnboarded(Message):
    """A project was initialised and is ready to be selected."""

    def __init__(self, project_id: str, path: Path) -> None:
        self.project_id = project_id
        self.path = path
        super().__init__()


class OnboardFailed(Message):
    """Onboarding stopped; ``reason`` is what to show the user."""

    def __init__(self, path: Path, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__()


class OnboardView(Vertical):
    """Directory picker + path input + a live log of `init` and `doctor`."""

    DEFAULT_CSS = """
    OnboardView { padding: 1 2; }
    OnboardView #onboard-path { margin-bottom: 1; }
    """

    def compose(self) -> ComposeResult:
        yield Static("Onboard a project — type a directory, or browse:", classes="hint")
        yield Input(placeholder="~/Code/your-repo", id="onboard-path")
        yield Static("", id="onboard-log")
