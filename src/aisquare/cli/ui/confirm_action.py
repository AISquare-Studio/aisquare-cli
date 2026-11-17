"""One box before an agent action, with *Don't ask again* (R17): Stop, Pause and Restart.

The owner (2026-11-16): the agent view's actions "should have on click confirmation box, but
an option to not ask again that if ticked it does not ask and you can one click stop pause
or restart without confirmation". :class:`ConfirmActionScreen` is that box: a question with
the agent's name as data, one or two lines of what happens in plain words (never a command:
R13), the action's button (Stop adds *Force*), Cancel, and the *Don't ask again* tick.

**The tick** is per KIND of action (``stop``, ``pause``, ``restart``), never per agent. It is
saved when the action's button (or *Force*) is pressed with it ticked, whatever the action
then answers: it answers the question, not the action. Cancel and Escape save nothing. From
then on that kind acts on one click, everywhere it is offered (the header, an agent's ⋯
menu, the sidebar's ``x`` for Stop): :func:`ask_first` reads ``[ui] dont_ask`` at each press
(``settings.asks_first``), so a tick in one asq holds in another on the same home at once.
*Ask again before every action* (App settings ▸ User preferences ▸ Advanced) clears every
kind, those a later release adds included.

A kind's widget ids are its own (``#pause-confirm``, ``#pause-dont-ask`` …), so the Stop box
keeps the ids it always had (``cli/ui/stop.py``, a subclass that runs the stop itself and
stays open on a refusal).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar, Generic, TypeVar

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Static

from aisquare.cli.ui.dont_ask import (
    ASK_AGAIN,
    ASK_AGAIN_WHERE,
    PAUSE,
    RESTART,
    STOP,
    said_not_asking,
    verb_of,
)
from aisquare.cli.ui.views.app_settings import UiSettingsChanged
from aisquare.core.config import UiSettings
from aisquare.services import settings as settings_service
from aisquare.services.fleet import FleetError

ResultT = TypeVar("ResultT")

__all__ = [
    "ASK_AGAIN",
    "ASK_AGAIN_WHERE",
    "PAUSE",
    "RESTART",
    "STOP",
    "ConfirmActionScreen",
    "ask_first",
    "said_not_asking",
    "verb_of",
]

_BOX_CSS = """
ConfirmActionScreen { align: center middle; }
ConfirmActionScreen > Vertical { width: 72; max-width: 92%; height: auto; border: heavy $accent;
                                 background: $surface; padding: 0 1; }
ConfirmActionScreen .picker-header { height: auto; padding: 1 0; }
ConfirmActionScreen .picker-note { height: auto; }
ConfirmActionScreen .picker-buttons { height: auto; align-horizontal: right; padding-bottom: 1; }
ConfirmActionScreen .picker-buttons Button { margin-left: 2; min-width: 10; }
ConfirmActionScreen .dont-ask { border: none; height: 1; padding: 0 1; margin-bottom: 1; }
ConfirmActionScreen .dont-ask:focus { text-style: bold reverse; }
"""


class ConfirmActionScreen(ModalScreen[ResultT], Generic[ResultT]):
    """The question, what happens, the action's button, Cancel and *Don't ask again*.

    As it is, it answers ``True`` at the action's button and ``None`` at Cancel or Escape,
    and the caller acts (:func:`ask_first`). The Stop box overrides :meth:`confirmed` to run
    the stop itself, and :meth:`extra_buttons` for *Force*. The action's button has the focus
    on open, so Enter confirms."""

    DEFAULT_CSS = _BOX_CSS
    BINDINGS: ClassVar = [Binding("escape", "cancel", "cancel")]
    AUTO_FOCUS = ".action-confirm"
    """The action's button, never the tick first in the box: Enter acts. The app's ``*`` focused
    the first focusable, the tick, and Enter ticked it (review of #477, M1)."""

    def __init__(self, kind: str, question: Text | None = None) -> None:
        super().__init__()
        self.kind = kind
        self._question = question if question is not None else Text()
        self.saved_ui: UiSettings | None = None
        """``[ui]`` as *Don't ask again* saved it, once it has; ``None`` until then."""

    @property
    def dont_ask_id(self) -> str:
        return f"{self.kind}-dont-ask"

    def question(self) -> Text:
        """The question and what happens, as data; a subclass builds its own."""
        return self._question

    def extra_buttons(self) -> list[Button]:
        """Buttons between the action's and Cancel: none here; Stop's *Force*."""
        return []

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.question(), classes="picker-header", id=f"{self.kind}-question")
            yield Static(id=f"{self.kind}-status", classes="picker-note")
            yield Checkbox("Don't ask again", False, id=self.dont_ask_id, classes="dont-ask")
            with Horizontal(classes="picker-buttons"):
                yield Button(
                    verb_of(self.kind),
                    id=f"{self.kind}-confirm",
                    variant="primary",
                    classes="action-confirm",
                )
                yield from self.extra_buttons()
                yield Button("Cancel", id=f"{self.kind}-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        pressed = event.button.id or ""
        if pressed == f"{self.kind}-cancel":
            self.action_cancel()
        elif pressed.startswith(f"{self.kind}-"):
            # The action's button or an extra one (Force): the tick is the answer to the box.
            self.confirmed(pressed.removeprefix(f"{self.kind}-"))

    def confirmed(self, which: str) -> None:
        """The action's button (``which`` is ``"confirm"``) or an extra one: the tick saved,
        then the answer."""
        self._save_dont_ask()
        self.dismiss(True)  # type: ignore[arg-type]

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _save_dont_ask(self) -> None:
        """Ticked at the action's button: the kind joins ``[ui] dont_ask``, once, and the shell
        is told (``UiSettingsChanged``), as App settings shows the reset's line from it. An
        unreadable file is left as it is, and the action goes on regardless."""
        if self.saved_ui is not None or not self.query_one(f"#{self.dont_ask_id}", Checkbox).value:
            return
        verb = verb_of(self.kind)
        try:
            self.saved_ui = settings_service.stop_asking_first(self.kind)
        except Exception as exc:
            reason = str(exc) if isinstance(exc, FleetError) else f"{type(exc).__name__}: {exc}"
            self.app.notify(
                f"Don't ask again was not saved, so {verb} will ask again: {reason}",
                severity="warning",
                timeout=8,
                markup=False,
            )
            return
        self.app.post_message(UiSettingsChanged(self.saved_ui))
        self.app.notify(
            f"{verb} will not ask again. {ASK_AGAIN_WHERE} turns it back on.",
            timeout=10,
            markup=False,
        )

    def _note(self, text: str | None, *, style: str = "bold red") -> None:
        note = self.query_one(f"#{self.kind}-status", Static)
        note.update(Text(text, style=style) if text else "")
        note.display = bool(text)


def ask_first(app: App[Any], screen: ConfirmActionScreen[Any], then: Callable[[], object]) -> bool:
    """``then`` once ``screen`` is confirmed, or at once when its kind does not ask
    (``[ui] dont_ask``, read now). Whether the box was shown."""
    if not settings_service.asks_first(screen.kind):
        then()
        return False

    def answered(confirmed: object) -> None:
        if confirmed:
            then()

    app.push_screen(screen, answered)
    return True
