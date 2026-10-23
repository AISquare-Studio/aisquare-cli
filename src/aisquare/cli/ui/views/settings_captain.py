"""Settings' *Captain*: the experimental captain switched on and off while asq runs (#33).

One box, *Captain (experimental)*. A tick saves ``[experimental] captain`` through
``core.config.save_config``, the one writer, which is what ``aisquare config set
experimental.captain true`` writes, and posts :class:`CaptainSwitched`. The shell then
shows or hides the ``★`` and the Captain section and starts or stops the captain's part
of its fleet read and its ui receiver, at once (``FleetApp.on_captain_switched``). It
took ``config set`` and a restart, which the owner could not find.

``AISQUARE_EXPERIMENTAL_CAPTAIN`` wins over the config (``core.experimental``). While it is
set, the box shows the state it forces, greyed, and one line says why: a tick would save
a setting the variable overrides.

Self-contained, so a later Settings layout moves it with the line that mounts it
(``views.settings_hub``).
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Checkbox, Static

from aisquare.core import experimental
from aisquare.core.config import load_config, save_config

ABOUT = (
    "the home-level agent that unblocks your agents: ★ beside Fleet, and its section above "
    "the projects"
)
"""The line under the box while the config decides."""


def forced_text(on: bool) -> str:
    """The line under the box while :data:`~aisquare.core.experimental.CAPTAIN_ENV` decides."""
    state = "on" if on else "off"
    return (
        f"{experimental.CAPTAIN_ENV} is set where asq runs and keeps the captain {state}: "
        "it wins over this setting"
    )


class CaptainSwitched(Message):
    """The *Captain* box saved a new state: the shell shows it at once (``FleetApp``)."""

    def __init__(self, on: bool) -> None:
        self.on = on
        super().__init__()


class CaptainToggle(Vertical):
    """*Captain (experimental)*: a box that saves the setting and posts :class:`CaptainSwitched`."""

    DEFAULT_CSS = """
    CaptainToggle { height: auto; margin-bottom: 1; }
    CaptainToggle #captain-toggle-title { height: 1; text-style: bold; margin: 1 0 1 0; }
    CaptainToggle #captain-toggle-about { height: auto; color: $text-muted; }
    """

    def compose(self) -> ComposeResult:
        forced = experimental.captain_forced()
        # What the shell shows now, which a config set from a shell meanwhile has not moved.
        shown = bool(getattr(self.app, "captain_on", experimental.captain_enabled()))
        yield Static(Text("Captain"), id="captain-toggle-title")
        yield Checkbox(
            "Captain (experimental)",
            value=shown if forced is None else forced,
            disabled=forced is not None,
            id="captain-toggle",
            compact=True,
        )
        about = ABOUT if forced is None else forced_text(forced)
        yield Static(Text(about), id="captain-toggle-about")

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if event.checkbox.id != "captain-toggle":
            return
        event.stop()
        problem = self._save(event.value)
        if problem is not None:
            self.notify(problem, severity="error", timeout=8, markup=False)
            with self.prevent(Checkbox.Changed):
                event.checkbox.value = not event.value
            return
        self.post_message(CaptainSwitched(experimental.captain_enabled()))

    @staticmethod
    def _save(on: bool) -> str | None:
        """Write ``[experimental] captain``; why not, when the config would not take it."""
        try:
            config = load_config()
        except Exception as exc:  # a broken config.toml: say so, change nothing
            return f"config unreadable — the captain is left as it was: {exc}"
        config.experimental.captain = on
        try:
            save_config(config)
        except OSError as exc:  # the operator's filesystem saying no
            return f"could not write the config — the captain is left as it was: {exc}"
        return None
