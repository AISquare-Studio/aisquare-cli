"""The Welcome view — what the right pane shows before anything is selected."""

from __future__ import annotations

import shutil

from rich.text import Text
from textual.widgets import Static


def presence_lines() -> Text:
    """Which of the tools the fleet leans on are on this machine."""
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
    return text


class WelcomeView(Static):
    """A short orientation plus the tool presence check."""

    DEFAULT_CSS = """
    WelcomeView { padding: 1 2; }
    """

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
            "\nF12 hands focus back to the sidebar from an agent pane · t themes · q quits",
            style="dim",
        )
        self.update(text)
