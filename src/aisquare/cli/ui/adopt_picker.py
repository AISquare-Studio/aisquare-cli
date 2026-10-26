"""*Adopt a conversation…*: ``fleet adopt --list`` as a picker, on the Accounts page (#290).

The conversations no fleet agent owns, newest first, as ``services.adopt.recent`` reads them
(off the UI thread, before this opens). One a live ``claude`` still holds, or whose directory
is no onboarded project, is listed with why and cannot be picked: adopting it would only be
refused. Enter on one answers its session id, which the page adopts in a worker; Esc answers
``None``.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import ClassVar

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from aisquare.models import AdoptableConversation


def _ago(now: datetime, then: datetime) -> str:
    seconds = max(int((now - then).total_seconds()), 0)
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def conversation_text(conversation: AdoptableConversation, now: datetime) -> Text:
    """One row: short id, account, directory, age; the first prompt under it; why it cannot
    be adopted, when it cannot. A Text, never markup: every field is the user's."""
    text = Text(conversation.session_id[:8], style="bold")
    text.append(f"  {conversation.account}  {conversation.cwd or '—'}  ")
    text.append(_ago(now, conversation.last_activity), style="dim")
    if conversation.first_prompt:
        text.append(f"\n  {conversation.first_prompt}", style="dim")
    if conversation.held_by is not None:
        text.append(f"\n  a claude still runs it (pid {conversation.held_by})", style="yellow")
    elif conversation.project_id is None:
        text.append("\n  its directory is no onboarded project", style="yellow")
    return text


def adoptable(conversation: AdoptableConversation) -> bool:
    """Whether adopting it could succeed: nothing holds it and a project holds its directory."""
    return conversation.held_by is None and conversation.project_id is not None


class AdoptPicker(ModalScreen[str | None]):
    """Pick a conversation to bring under the fleet; answers its session id or ``None``."""

    CSS = """
    AdoptPicker { align: center middle; }
    #adoptbox { width: 90%; max-width: 110; height: auto; max-height: 80%;
                border: heavy $accent; background: $surface; padding: 1; }
    #adopthint { height: auto; color: $text-muted; }
    #adoptlist { height: auto; max-height: 24; }
    """
    BINDINGS: ClassVar = [("escape", "close_picker", "close")]

    def __init__(self, conversations: Sequence[AdoptableConversation], now: datetime) -> None:
        super().__init__()
        self._conversations = list(conversations)
        self._now = now

    def compose(self) -> ComposeResult:
        with Vertical(id="adoptbox"):
            hint = (
                "adopt a conversation into the fleet — ↑/↓ then Enter · Esc cancels"
                if self._conversations
                else "no conversation outside the fleet on the accounts the CLI knows · Esc"
            )
            yield Static(hint, id="adopthint")
            yield OptionList(id="adoptlist")

    def on_mount(self) -> None:
        picker = self.query_one("#adoptlist", OptionList)
        for conversation in self._conversations:
            picker.add_option(
                Option(
                    conversation_text(conversation, self._now),
                    id=conversation.session_id,
                    disabled=not adoptable(conversation),
                )
            )
        picker.focus()

    @on(OptionList.OptionSelected, "#adoptlist")
    def _chosen(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_close_picker(self) -> None:
        self.dismiss(None)
