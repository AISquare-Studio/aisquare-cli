"""The Welcome view — what the right pane shows before anything is selected.

docs/plans/fleet-tui.md §4.2: what this is, ``+`` to add a project, and an
inline presence check for the tools the fleet leans on, with install hints for
the ones that are missing. It reads nothing from the store; the sidebar is the
project list.
"""

from __future__ import annotations

import shutil

from rich.text import Text
from textual.widgets import Static

INSTALL_HINT: dict[str, str] = {
    "tmux": "apt install tmux · dnf install tmux · brew install tmux",
    "claude": "npm install -g @anthropic-ai/claude-code",
    "gh": "https://cli.github.com",
}


def presence_lines() -> Text:
    """Which of the tools the fleet leans on are on this machine, with a hint per gap."""
    text = Text()
    for tool, why in (
        ("tmux", "the fleet's session substrate — agents run inside it"),
        ("claude", "the agent every fleet role runs on"),
        ("gh", "PRs for the coder and reviewer"),
    ):
        found = shutil.which(tool)
        mark = "✓" if found else "✗"
        text.append(f"  {mark} {tool:<7}", style="green" if found else "red")
        text.append(f" {why}\n", style="dim")
        if not found:
            text.append(f"            install: {INSTALL_HINT[tool]}\n", style="dim italic")
    return text


class WelcomeView(Static):
    """A short orientation plus the tool presence check."""

    DEFAULT_CSS = """
    WelcomeView { padding: 1 2; }
    """

    def __init__(self, *, escape_key: str = "f12", id: str | None = None) -> None:
        super().__init__(id=id)
        self.escape_key = escape_key

    def on_mount(self) -> None:
        text = Text()
        text.append("aisquare fleet\n", style="bold")
        text.append(
            "Every project, its manager and the agents it spawns — each a real session,\n"
            "surfaced here. Press + in the sidebar to onboard a project, or click one.\n\n",
        )
        text.append("On this machine:\n", style="bold")
        text.append(presence_lines())
        text.append(
            f"\n{self.escape_key.upper()} hands focus back to the sidebar from an agent pane"
            " · t themes · r refresh · ? help · q quits",
            style="dim",
        )
        self.update(text)
