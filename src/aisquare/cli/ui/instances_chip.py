"""``2 asq · Mac + ssh (here)`` under the status line while more than one asq runs on this
home (#20); hidden while this one is alone.

The owner runs asq at the Mac and over SSH into the same Mac at once, and nothing on screen
said so. The chip names where each live asq runs (``core.instances``) and which one is this.
It is read with the fleet's frame, on that read's thread (``FleetApp._paint_fleet``): no timer
of its own, which on Windows would hold an executor thread for the app's whole life
(``HOUSEKEEPING_SECONDS``).
"""

from __future__ import annotations

import sys
from collections import Counter
from collections.abc import Sequence

from rich.text import Text
from textual.widgets import Static

from aisquare.core.instances import Instance, host_name

HERE = "here"
"""The word that marks this asq's place in the chip."""


def place(instance: Instance, *, host: str) -> str:
    """Where ``instance`` runs, in a word: ``ssh`` for one reached from another machine,
    ``Mac`` (``local`` off macOS) for one at this machine, and the host's name for one at
    another machine sharing the home."""
    if instance.kind == "ssh":
        return "ssh"
    if instance.host != host:
        return instance.host.split(".", 1)[0]
    return "Mac" if sys.platform == "darwin" else "local"


def chip_text(
    live: Sequence[Instance], this_id: str | None, *, host: str | None = None
) -> Text | None:
    """The chip's line for the live asqs ``live``, ``this_id`` being this asq's record (``None``:
    this process is not registered, and nothing is marked); ``None`` with fewer than two.

    One word per place, the machine's own first, with a count where two share one
    (``3 asq · Mac + 2 ssh``); this asq's place is marked ``(here)``, in bold."""
    if len(live) < 2:
        return None
    here = host_name() if host is None else host
    places = Counter(place(instance, host=here) for instance in live)
    mine = next((place(i, host=here) for i in live if i.id == this_id), None)
    order = sorted(places, key=lambda word: (word == "ssh", word not in ("Mac", "local"), word))
    text = Text(f"{len(live)} asq · ")
    for index, word in enumerate(order):
        if index:
            text.append(" + ")
        count = places[word]
        shown = f"{count} {word}" if count > 1 else word
        if word == mine:
            text.append(f"{shown} ({HERE})", style="bold")
        else:
            text.append(shown)
    if differ(live):
        text.append(" ⚠")
    return text


def differ(live: Sequence[Instance]) -> bool:
    """Whether two of the live asqs run different releases or store schemas: what the gate
    refuses, so only an asq that came up past it (none should) or one from before it."""
    return any(instance.differs_from(live[0]) for instance in live[1:])


def chip_tooltip(live: Sequence[Instance], this_id: str | None) -> str:
    """Each live asq on a line of its own (``Instance.label`` and since when), this one marked."""
    lines = ["asq running on this home:"]
    for instance in live:
        mark = "  ← this asq" if instance.id == this_id else ""
        lines.append(f"• asq {instance.version} — {instance.where}{mark}")
    if differ(live):
        lines.append("They run different releases: quit the older one (q in it).")
    lines.append("`aisquare instances` lists them.")
    return "\n".join(lines)


class InstancesChip(Static):
    """The chip: shown while two or more asqs are live on this home (:func:`chip_text`). Not a
    row: never under the keyboard cursor, as the Remote chip is not."""

    DEFAULT_CSS = """
    InstancesChip { height: 1; color: $accent; text-wrap: nowrap; text-overflow: ellipsis; }
    InstancesChip.-differ { color: $warning; }
    """

    ALLOW_SELECT = False

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(Text(""), id=id)
        self.live: tuple[Instance, ...] = ()
        """The live asqs it last showed."""

    def on_mount(self) -> None:
        self.display = False

    def show_instances(self, live: Sequence[Instance], this_id: str | None) -> None:
        self.live = tuple(live)
        text = chip_text(self.live, this_id)
        self.display = text is not None
        self.set_class(text is not None and differ(self.live), "-differ")
        if text is None:
            self.tooltip = None
            return
        self.update(text)
        self.tooltip = chip_tooltip(self.live, this_id)
