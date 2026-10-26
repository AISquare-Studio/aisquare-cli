"""Settings' *Captain*: the experimental captain switched on and off while asq runs (#33).

One box, *Captain (experimental)*. A tick saves ``[experimental] captain`` through
``core.config.save_config``, the one writer, which is what ``aisquare config set
experimental.captain true`` writes, and posts :class:`CaptainSwitched`. The shell then
shows or hides *Start captain* (the captain's row while it runs) and starts or stops the
captain's part of its fleet read and its ui receiver, at once
(``FleetApp.on_captain_switched``). It took ``config set`` and a restart, which the owner
could not find.

``AISQUARE_EXPERIMENTAL_CAPTAIN`` wins over the config (``core.experimental``). While it is
set, the box shows the state it forces, greyed, and one line says why: a tick would save
a setting the variable overrides.

It tops App settings' *Agent settings* tab (the owner's placement, #41), in that form's
format: a section heading, the box as a row, a dim help line, styled by the form's own
``.section``/``.row``/``.about`` rules. It saves at once, as it did, not with the form's
*Save*; the form's Reload and Reset all re-read the file under it
(:meth:`CaptainToggle.reread`), so the box and the shell follow what the file holds.
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Checkbox, Static

from aisquare.core import experimental
from aisquare.core.config import AppConfig, load_config, save_config

ABOUT = (
    "the home-level agent that unblocks your agents: Start captain at the top of the sidebar, "
    "and its row there while it runs"
)
"""The line under the box while the config decides."""
TITLE = ("Captain", "experimental")
"""The section heading: its name, and what it is, dim (``app_settings.section_title``)."""


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
    CaptainToggle { height: auto; }
    """

    def compose(self) -> ComposeResult:
        # Here, not at the top: App settings, which holds the form's format, mounts this.
        from aisquare.cli.ui.views.app_settings import section_title

        forced = experimental.captain_forced()
        # What the shell shows now, which a config set from a shell meanwhile has not moved.
        shown = bool(getattr(self.app, "captain_on", experimental.captain_enabled()))
        yield Static(section_title(*TITLE), id="captain-toggle-title", classes="section")
        with Horizontal(classes="row preference"):
            yield Checkbox(
                "Captain (experimental)",
                value=shown if forced is None else forced,
                disabled=forced is not None,
                id="captain-toggle",
                compact=True,
            )
        about = ABOUT if forced is None else forced_text(forced)
        yield Static(Text(about), id="captain-toggle-about", classes="about")

    def reread(self, config: AppConfig) -> None:
        """The form re-read the file (Reload, Save, Reset all): the box shows what it holds,
        and the shell follows when that moved the switch — Reset all turned a ticked captain
        off in the file while Start captain and the captain's row stayed until a restart. While
        the variable decides, the box keeps the state it forces."""
        if experimental.captain_forced() is not None:
            return
        box = self.query_one("#captain-toggle", Checkbox)
        with self.prevent(Checkbox.Changed):
            box.value = config.experimental.captain
        if config.experimental.captain != bool(getattr(self.app, "captain_on", box.value)):
            # To the app itself, not bubbled: Reset all re-reads from its dialog's callback,
            # which runs as the app, and Textual never bubbles a message up to its sender.
            self.app.post_message(CaptainSwitched(config.experimental.captain))

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
