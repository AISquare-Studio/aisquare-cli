"""The Welcome view — what the right pane shows before anything is selected.

docs/plans/fleet-tui.md §4.2: what this is, ``+`` to add a project, and an
inline presence check for the tools the fleet leans on, with install hints for
the ones that are missing. It reads nothing from the store; the sidebar is the
project list.
"""

from __future__ import annotations

import asyncio
import shutil

from rich.text import Text
from textual.widgets import Static

INSTALL_HINT: dict[str, str] = {
    "tmux": "apt install tmux · dnf install tmux · brew install tmux",
    "gh": "https://cli.github.com",
}


def presence_lines() -> Text:
    """Which of the tools the fleet leans on are on this machine, with a hint per gap."""
    from aisquare.services import agent_launch

    text = Text()
    tools = [
        (
            "tmux",
            "the fleet's session substrate — agents run inside it",
            shutil.which("tmux"),
            f"install: {INSTALL_HINT['tmux']}",
        )
    ]
    try:
        selected = agent_launch.resolve()
    except ValueError as exc:
        tools.append(
            (
                "coding agent:",
                str(exc),
                None,
                exc.fix
                if isinstance(exc, agent_launch.UnknownWrapperError)
                else "Fix the agent selection in Settings or aisquare agents use.",
            )
        )
    else:
        tools.append(
            (
                selected.binary.binary,
                f"selected coding agent ({selected.adapter.label})",
                agent_launch.executable(selected),
                f"install: {selected.adapter.install_hint}",
            )
        )
    tools.append(
        (
            "gh",
            "PRs for the coder and reviewer",
            shutil.which("gh"),
            f"install: {INSTALL_HINT['gh']}",
        )
    )
    for tool, why, found, hint in tools:
        mark = "✓" if found else "✗"
        text.append(f"  {mark} {tool:<7}", style="green" if found else "red")
        text.append(f" {why}\n", style="dim")
        if not found:
            text.append(
                f"            {hint}\n",
                style="dim italic",
            )
    return text


class WelcomeView(Static):
    """A short orientation plus the tool presence check."""

    DEFAULT_CSS = """
    WelcomeView { padding: 1 2; }
    """

    def __init__(self, *, escape_key: str = "f12", id: str | None = None) -> None:
        super().__init__(id=id)
        self.escape_key = escape_key

    async def on_mount(self) -> None:
        text = Text()
        text.append("aisquare fleet\n", style="bold")
        text.append(
            "Every project, its manager and the agents it spawns — each a real session,\n"
            "surfaced here. Press + in the sidebar to onboard a project, or click one.\n\n",
        )
        text.append("On this machine:\n", style="bold")
        text.append(await asyncio.to_thread(presence_lines))
        text.append(
            f"\n{self.escape_key.upper()} hands focus back to the sidebar from an agent pane"
            " · t themes · r refresh · ? help · q quits",
            style="dim",
        )
        self.update(text)
