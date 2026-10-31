"""The Agent resources page: what each live agent consumes, read again while the page is open.

Opened from *⚙ Settings ▸ Agent resources*. The table ``aisquare fleet top`` prints
(``cli.fleet_top_render``): per live agent of every project, the CPU, memory and processes of
its pane's process tree, its uptime, and its session's tokens and cost so far, heaviest
first, with a totals row. Sorted by CPU, memory or tokens — keys ``1`` to ``3`` or the
buttons; ``r`` reads again now.

**It samples only while it shows.** Showing the page starts a timer that samples every
:data:`REFRESH_SECONDS`; hiding it (another page, another row) stops the timer, so a page left
behind costs nothing. A sample runs off the UI thread (:data:`SAMPLE_WORKER`), one at a time: a
tick that finds one still running skips. A sort repaints the last sample, it reads nothing.

**Read-only.** Nothing on the page stops, kills or restarts anything.

``sampler`` is how the page reads: ``() -> TopReport``, raising for "no answer" (the page keeps
what it showed and says why). The default keeps one ``fleet_top.SpendReader`` for the page's
life, so each sample scans only what the transcripts gained since the last. Tests hand it a
fake.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import ClassVar

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.timer import Timer
from textual.widgets import Button, Static
from textual.worker import Worker, WorkerState

from aisquare.cli import fleet_top_render as render
from aisquare.cli.ui.ticks import while_running
from aisquare.services import fleet_top
from aisquare.services.fleet_top import SortKey, TopReport

REFRESH_SECONDS = 5.0
"""How often the page samples while it shows."""

SAMPLE_WORKER = "agent-resources-sample"
"""The worker a sample runs in: one at a time."""

KEYS: dict[SortKey, str] = {"cpu": "1", "mem": "2", "tokens": "3"}
"""The key of each sort."""

BUTTON_LABELS: dict[SortKey, str] = {"cpu": "CPU", "mem": "Memory", "tokens": "Tokens"}

EMPTY = "No live agents — spawn one from a project, and what it consumes shows here."

Sampler = Callable[[], TopReport]


def default_sampler() -> Sampler:
    """Every live agent's resources, with one spend reader kept across samples."""
    spend = fleet_top.SpendReader()
    return lambda: fleet_top.sample(spend=spend)


class ResourcesView(VerticalScroll):
    """Settings ▸ Agent resources: a title, the sort switcher, a status line and the table."""

    DEFAULT_CSS = """
    ResourcesView { padding: 1 2; }
    ResourcesView #resources-title { height: 1; text-style: bold; margin-bottom: 1; }
    ResourcesView #resources-sorts { height: auto; margin-bottom: 1; }
    ResourcesView .resources-sort { margin: 0 1 0 0; min-width: 12; }
    ResourcesView #resources-status { height: auto; color: $text-muted; margin-bottom: 1; }
    ResourcesView #resources-table { height: auto; }
    ResourcesView #resources-notes { height: auto; color: $text-muted; margin-top: 1; }
    """

    BINDINGS: ClassVar = [
        Binding("1", "sort('cpu')", "CPU", show=False),
        Binding("2", "sort('mem')", "memory", show=False),
        Binding("3", "sort('tokens')", "tokens", show=False),
        Binding("r", "sample_now", "refresh", show=False),
    ]

    def __init__(self, *, sampler: Sampler | None = None, id: str | None = None) -> None:
        super().__init__(id=id)
        self._sampler = sampler or default_sampler()
        self.sort: SortKey = "cpu"
        self.report: TopReport | None = None
        """The newest sample painted."""
        self._timer: Timer | None = None

    def compose(self) -> ComposeResult:
        yield Static(Text("Agent resources"), id="resources-title")
        with Horizontal(id="resources-sorts"):
            for key in fleet_top.SORT_KEYS:
                yield Button(
                    f"{KEYS[key]}  {BUTTON_LABELS[key]}",
                    id=f"resources-sort-{key}",
                    classes="resources-sort",
                    compact=True,
                )
        yield Static(Text("Reading the agents' processes…"), id="resources-status")
        yield Static("", id="resources-table")
        yield Static("", id="resources-notes")

    def on_mount(self) -> None:
        self._paint_sorts()

    # --- sampling: only while the page shows --------------------------------------------

    @property
    def sampling(self) -> bool:
        """Whether the page's timer runs: it does while the page shows, and only then."""
        return self._timer is not None

    def on_show(self) -> None:
        if self._timer is None:
            self._timer = self.set_interval(REFRESH_SECONDS, self.tick)
        self.tick()

    def on_hide(self) -> None:
        self._stop()

    def on_unmount(self) -> None:
        self._stop()

    def _stop(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    @property
    def busy(self) -> bool:
        """Whether a sample is running."""
        return any(w.name == SAMPLE_WORKER and not w.is_finished for w in self.workers)

    @while_running
    def tick(self) -> None:
        """Sample now, off the UI thread — unless a sample still runs (a slow ``ps``, many
        transcripts the first time): the next tick asks again."""
        if self.busy:
            return
        self.run_worker(
            self._sampler,
            name=SAMPLE_WORKER,
            group=SAMPLE_WORKER,
            thread=True,
            exit_on_error=False,
        )

    def action_sample_now(self) -> None:
        self.tick()

    @on(Worker.StateChanged)
    def _sampled(self, event: Worker.StateChanged) -> None:
        worker = event.worker
        if worker.name != SAMPLE_WORKER:
            return
        if event.state is WorkerState.ERROR:
            self.query_one("#resources-status", Static).update(
                Text(f"Could not read the agents: {worker.error}", style="red")
            )
            return
        if event.state is WorkerState.SUCCESS and isinstance(worker.result, TopReport):
            self.report = worker.result
            self.paint()

    # --- sorting -------------------------------------------------------------------------

    def action_sort(self, key: SortKey) -> None:
        """Sort by ``key``: the key, or the button. Repaints the last sample; reads nothing."""
        self.sort = key
        self._paint_sorts()
        self.paint()

    @on(Button.Pressed, ".resources-sort")
    def _sort_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        wanted = str(event.button.id).removeprefix("resources-sort-")
        key = next((key for key in fleet_top.SORT_KEYS if key == wanted), None)
        if key is not None:
            self.action_sort(key)

    def _paint_sorts(self) -> None:
        for key in fleet_top.SORT_KEYS:
            button = self.query_one(f"#resources-sort-{key}", Button)
            button.variant = "primary" if key == self.sort else "default"

    # --- painting ------------------------------------------------------------------------

    def paint(self) -> None:
        """Draw :attr:`report` sorted by :attr:`sort`; nothing before the first sample."""
        report = self.report
        if report is None:
            return
        status = render.summary(report, self.sort)
        at = report.sampled_at.astimezone().strftime("%H:%M:%S")
        self.query_one("#resources-status", Static).update(
            Text(f"{status} · at {at}, every {REFRESH_SECONDS:g} s while open")
        )
        table = self.query_one("#resources-table", Static)
        notes = self.query_one("#resources-notes", Static)
        if not report.agents:
            table.update(Text(EMPTY))
            notes.update("")
            return
        table.update(render.top_table(report, self.sort))
        notes.update(Text("\n".join(render.notes(report))))
