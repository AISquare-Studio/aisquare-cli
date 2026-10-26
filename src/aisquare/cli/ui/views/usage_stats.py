"""The Usage & stats page: what this machine's Claude Code sessions spent, in a range.

Opened from *⚙ Settings ▸ Usage & stats*. Under the title, the range switcher — *Today*,
*7 days*, *30 days*, *Lifetime*, keys ``1`` to ``4`` — then the figures of that range
(``services.usage_stats.stats``, the model ``aisquare metrics stats`` prints): tiles for
tokens, cost at list prices, sessions, turns, active days and repositories; a bar per day (for
*Today*, a bar per hour); when the work happens (weekdays, hours); the ten repositories with
the most tokens; the accounts; the busiest days; the models; and the features used most.

**Everything is read off the UI thread** (:data:`LOAD_WORKER`). Opening the page also sweeps
Claude Code's transcripts for sessions no hook saw (``usage_stats.maybe_sweep``, at most once
in ten minutes, the first time reading history that still exists), and says how far it is
while it reads; a range switch only reads what is recorded. With nothing recorded the page
says so and how to record it, instead of a page of zeros.

**The slot at the top** (``#usage-sharing-slot``, between the title and the range switcher) is
where the opt-in sharing switch goes (the next task of this feature). It is empty here: this
page records and shows, and nothing it does leaves the machine.

``loader`` is how the page asks for a range: ``(range, sweep, progress) -> UsageStats``,
raising for "no answer" (the page keeps what it showed and says why). Tests hand it a fake.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import ClassVar

from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Static
from textual.worker import Worker, WorkerState

from aisquare.cli import usage_format as fmt
from aisquare.models import USAGE_RANGES, UsageRange, UsageStats
from aisquare.services import usage_stats as usage_service

LOAD_WORKER = "usage-stats-load"
"""The worker a read of the stats (and, on opening the page, the sweep before it) runs in."""

Progress = Callable[[int, int], None]
Loader = Callable[[UsageRange, bool, Progress | None], UsageStats]

KEYS: dict[UsageRange, str] = {"today": "1", "7d": "2", "30d": "3", "lifetime": "4"}
"""The key of each range."""
DEFAULT_RANGE: UsageRange = "7d"
CHART_HEIGHT = 7
CHART_WIDTH = 72
"""The widest the per-day chart is drawn, in cells."""

TILES: tuple[tuple[str, str], ...] = (
    ("tokens", "tokens"),
    ("io", "input + output"),
    ("input", "input only"),
    ("cost", "cost at list prices"),
    ("sessions", "sessions"),
    ("turns", "turns"),
    ("days", "active days"),
    ("repos", "repositories"),
)
"""The tiles, row by row: each one's id stem and label."""

EMPTY = (
    "No usage recorded yet.\n\n"
    "Claude Code's transcripts are read when this page opens, and each session aisquare "
    "launches adds its turns as it goes. If you use Claude Code and still see this, run "
    "`aisquare metrics backfill`."
)


def default_loader(range_: UsageRange, sweep: bool, progress: Progress | None) -> UsageStats:
    """The page's real read: the sweep when asked (throttled), then the range."""
    if sweep:
        usage_service.maybe_sweep(progress=progress)
    return usage_service.stats(range_)


def tile(value: str, label: str) -> Text:
    """One tile: the figure over what it counts."""
    text = Text(value, style="bold")
    text.append(f"\n{label}", style="dim")
    return text


def _heading(text: str) -> Text:
    return Text(text, style="bold")


def chart_text(stats: UsageStats, width: int = CHART_WIDTH) -> Text:
    """The page's chart as text: a column per local day (per week, month … once there are more
    than fit ``width``), or a column per hour for *Today*; the date range under it."""
    text = Text()
    if stats.range == "today":
        values, note = stats.by_hour, "turns per hour of the day"
        first, last, per = "0h", "23h", 1
        gap = 1
    else:
        series = [day.tokens.total for day in stats.days]
        values, per = fmt.rebucket(series, max(8, width - 8))
        note = "tokens per day" if per == 1 else f"tokens per {per} days"
        first, last = (stats.days[0].date, stats.days[-1].date) if stats.days else ("", "")
        gap = 1 if len(values) * 2 <= width else 0
    peak = max(values, default=0)
    text.append(f"{note}  (peak {fmt.tokens(peak)})\n", style="dim")
    for line in fmt.column_chart(values, height=CHART_HEIGHT, gap=gap):
        text.append(line + "\n", style="green")
    columns = len(values) * (1 + gap)
    text.append(first, style="dim")
    if columns > len(first) + len(last) + 1:
        text.append(" " * (columns - len(first) - len(last)))
    else:
        text.append(" ")
    text.append(last, style="dim")
    return text


def repos_table(stats: UsageStats) -> Table:
    table = Table(
        title=f"Top repositories — {stats.repos.active} active",
        title_justify="left",
        title_style="bold",
        box=None,
        pad_edge=False,
        expand=True,
    )
    table.add_column("repository", ratio=1, no_wrap=True, overflow="ellipsis")
    for column in ("tokens", "cost", "sessions", "turns", "last active"):
        table.add_column(column, justify="right", no_wrap=True)
    for row in stats.repos.rows:
        table.add_row(
            row.name,
            fmt.tokens(row.tokens.total),
            fmt.money(row.cost_usd),
            str(row.sessions),
            str(row.turns),
            fmt.short_date(row.last_active) if row.last_active else "",
        )
    return table


def accounts_table(stats: UsageStats) -> Table:
    accounts = stats.accounts
    plans = ", ".join(f"{count} {kind}" for kind, count in accounts.by_type.items())
    title = f"Accounts — {accounts.active} of {accounts.registered} active"
    if plans:
        title += f" ({plans})"
    table = Table(
        title=title, title_justify="left", title_style="bold", box=None, pad_edge=False,
        expand=True,
    )  # fmt: skip
    table.add_column("account", ratio=1, no_wrap=True, overflow="ellipsis")
    table.add_column("plan", no_wrap=True)
    for column in ("tokens", "share", "sessions"):
        table.add_column(column, justify="right", no_wrap=True)
    for row in accounts.rows:
        top = accounts.top is not None and row.slot == accounts.top.slot
        table.add_row(
            Text(row.label + (" ★" if top else ""), style="bold" if top else ""),
            row.subscription,
            fmt.tokens(row.tokens.total),
            fmt.percent(row.share),
            str(row.sessions),
        )
    return table


def busiest_text(stats: UsageStats) -> Text:
    text = Text()
    text.append("Busiest days\n", style="bold")
    peak = max((day.tokens.total for day in stats.busiest_days), default=0)
    for day in stats.busiest_days:
        text.append(f"{fmt.short_date(day.date):<11} ")
        text.append(f"{fmt.bar(day.tokens.total, peak, 16):<17}", style="green")
        text.append(f"{fmt.tokens(day.tokens.total):>7}  ")
        text.append(f"{day.turns} turns · {day.sessions} sessions\n", style="dim")
    return text


def when_text(stats: UsageStats) -> Text:
    text = Text()
    text.append("When you work", style="bold")
    text.append("  (turns)\n", style="dim")
    text.append("weekday  ")
    text.append(fmt.spark(stats.by_weekday), style="green")
    text.append("  " + " ".join(f"{n[:2]}" for n in fmt.WEEKDAYS) + "\n", style="dim")
    text.append("hour     ")
    text.append(fmt.spark(stats.by_hour), style="green")
    text.append("  0h → 23h", style="dim")
    return text


def models_text(stats: UsageStats) -> Text:
    text = Text()
    text.append("Models\n", style="bold")
    for row in stats.models:
        text.append(f"{row.model:<22} ")
        text.append(f"{fmt.bar(row.tokens.total, stats.tokens.total, 14):<15}", style="green")
        text.append(f"{fmt.percent(row.share):>5} {fmt.tokens(row.tokens.total):>7}  ")
        text.append(fmt.money(row.cost_usd) if row.priced else "unpriced", style="dim")
        text.append("\n")
    return text


def features_text(stats: UsageStats) -> Text:
    text = Text()
    text.append("Features you use most\n", style="bold")
    if not stats.features:
        text.append("none counted yet — commands and pages are counted as you use them", "dim")
        return text
    width = max(len(row.name) for row in stats.features)
    for row in stats.features:
        text.append(f"{row.name:<{width}}  {row.count:>6,}\n")
    return text


class UsageStatsView(VerticalScroll):
    """Settings ▸ Usage & stats: a title, the sharing slot, the range switcher and the figures."""

    DEFAULT_CSS = """
    UsageStatsView { padding: 1 2; }
    UsageStatsView #usage-title { height: 1; text-style: bold; margin-bottom: 1; }
    UsageStatsView #usage-sharing-slot { height: auto; }
    UsageStatsView #usage-ranges { height: auto; margin-bottom: 1; }
    UsageStatsView .usage-range { margin: 0 1 0 0; min-width: 12; }
    UsageStatsView #usage-status { height: auto; color: $text-muted; margin-bottom: 1; }
    UsageStatsView #usage-tiles { grid-size: 4; grid-gutter: 1 2; height: auto; }
    UsageStatsView .usage-tile { height: 3; padding: 0 1; border-left: tall $primary; }
    UsageStatsView #usage-split { height: auto; margin-top: 1; }
    UsageStatsView .usage-block { height: auto; margin-top: 1; }
    UsageStatsView #usage-empty { height: auto; margin-top: 1; }
    """

    BINDINGS: ClassVar = [
        Binding("1", "range('today')", "today", show=False),
        Binding("2", "range('7d')", "7 days", show=False),
        Binding("3", "range('30d')", "30 days", show=False),
        Binding("4", "range('lifetime')", "lifetime", show=False),
        Binding("r", "reload", "refresh", show=False),
    ]

    def __init__(self, *, loader: Loader = default_loader, id: str | None = None) -> None:
        super().__init__(id=id)
        self._loader = loader
        self.range: UsageRange = DEFAULT_RANGE
        self.stats: UsageStats | None = None
        """The newest stats painted, for the range :attr:`range` or the one before it."""

    def compose(self) -> ComposeResult:
        yield Static(Text("Usage & stats"), id="usage-title")
        # SHARING SLOT: the opt-in anonymous-sharing switch (task #5) mounts here, above the
        # figures. Left empty by design: nothing this page does sends anything anywhere.
        yield Vertical(id="usage-sharing-slot")
        with Horizontal(id="usage-ranges"):
            for range_ in USAGE_RANGES:
                yield Button(
                    f"{KEYS[range_]}  {fmt.RANGE_LABELS[range_]}",
                    id=f"usage-range-{range_}",
                    classes="usage-range",
                    compact=True,
                )
        yield Static("", id="usage-status")
        yield Static(Text(EMPTY), id="usage-empty")
        with Vertical(id="usage-body"):
            with Grid(id="usage-tiles"):
                for stem, label in TILES:
                    yield Static(tile("…", label), id=f"usage-tile-{stem}", classes="usage-tile")
            yield Static("", id="usage-breakdown", classes="usage-block")
            yield Static("", id="usage-chart", classes="usage-block")
            yield Static("", id="usage-when", classes="usage-block")
            yield Static("", id="usage-repos", classes="usage-block")
            yield Static("", id="usage-accounts", classes="usage-block")
            yield Static("", id="usage-busiest", classes="usage-block")
            yield Static("", id="usage-models", classes="usage-block")
            yield Static("", id="usage-features", classes="usage-block")

    def on_mount(self) -> None:
        self.query_one("#usage-body").display = False
        self.query_one("#usage-empty").display = False
        self._paint_ranges()
        self.query_one("#usage-status", Static).update(Text("Reading your usage…"))

    def on_show(self) -> None:
        """The page is opened: read it again (and sweep the transcripts, at most every ten
        minutes), on whatever range it was left on."""
        self.reload(sweep=True)

    # --- reading -----------------------------------------------------------------------

    def reload(self, *, sweep: bool = False) -> None:
        """Read :attr:`range` off the UI thread; with ``sweep``, sweep the transcripts first."""
        range_ = self.range
        app = self.app

        def progress(done: int, total: int) -> None:
            try:
                app.call_from_thread(self._reading, done, total)
            except Exception:  # the app is closing: nobody is waiting for the figure
                return

        self.run_worker(
            lambda: self._loader(range_, sweep, progress if sweep else None),
            name=LOAD_WORKER,
            group=LOAD_WORKER,
            exclusive=True,
            thread=True,
            exit_on_error=False,
        )

    def _reading(self, done: int, total: int) -> None:
        if total and done < total:
            self.query_one("#usage-status", Static).update(
                Text(f"Reading Claude Code's transcripts… {done}/{total}")
            )

    @on(Worker.StateChanged)
    def _loaded(self, event: Worker.StateChanged) -> None:
        if event.worker.name != LOAD_WORKER:
            return
        status = self.query_one("#usage-status", Static)
        if event.state is WorkerState.SUCCESS and isinstance(event.worker.result, UsageStats):
            stats = event.worker.result
            if stats.range != self.range:
                return  # a range asked for since: its own read paints
            self.paint(stats)
            status.update(Text(f"Updated {datetime.now().strftime('%H:%M:%S')}"))
        elif event.state is WorkerState.ERROR:
            status.update(Text(f"Could not read usage: {event.worker.error}", style="red"))

    # --- the ranges ----------------------------------------------------------------------

    def action_range(self, range_: UsageRange) -> None:
        """Show ``range_``: the key, or the button."""
        if range_ == self.range and self.stats is not None:
            return
        self.range = range_
        self._paint_ranges()
        self.reload()

    def action_reload(self) -> None:
        self.reload(sweep=True)

    @on(Button.Pressed, ".usage-range")
    def _range_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_range(str(event.button.id).removeprefix("usage-range-"))  # type: ignore[arg-type]

    def _paint_ranges(self) -> None:
        for range_ in USAGE_RANGES:
            button = self.query_one(f"#usage-range-{range_}", Button)
            button.variant = "primary" if range_ == self.range else "default"

    # --- painting ------------------------------------------------------------------------

    def paint(self, stats: UsageStats) -> None:
        """Draw ``stats``, or the empty state when it holds no turn."""
        self.stats = stats
        empty = not stats.turns
        self.query_one("#usage-empty").display = empty
        self.query_one("#usage-body").display = not empty
        if empty:
            return
        values = {
            "tokens": (fmt.tokens(stats.tokens.total), "tokens"),
            "io": (fmt.tokens(stats.tokens.input_output), "input + output"),
            "input": (fmt.tokens(stats.tokens.input), "input only"),
            "cost": (fmt.money(stats.cost_usd), "cost at list prices"),
            "sessions": (f"{stats.sessions:,}", "sessions"),
            "turns": (f"{stats.turns:,}", "turns"),
            "days": (f"{stats.active_days:,}", "active days"),
            "repos": (f"{stats.repos.active:,}", "repositories"),
        }
        for stem, (value, label) in values.items():
            self.query_one(f"#usage-tile-{stem}", Static).update(tile(value, label))
        tokens = stats.tokens
        breakdown = (
            f"input {fmt.tokens(tokens.input)} · output {fmt.tokens(tokens.output)} · "
            f"cache write {fmt.tokens(tokens.cache_write)} · "
            f"cache read {fmt.tokens(tokens.cache_read)}"
        )
        self._set("#usage-breakdown", Text(breakdown, style="dim"))
        self._set("#usage-chart", chart_text(stats, max(40, self.size.width - 6)))
        self._set("#usage-when", when_text(stats))
        self._set("#usage-repos", repos_table(stats))
        self._set("#usage-accounts", accounts_table(stats))
        self._set("#usage-busiest", busiest_text(stats))
        self._set("#usage-models", models_text(stats))
        self._set("#usage-features", features_text(stats))

    def _set(self, selector: str, content: Text | Table) -> None:
        self.query_one(selector, Static).update(content)
