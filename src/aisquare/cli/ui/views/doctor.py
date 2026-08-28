"""The Doctor view: one project's checks with their fixes (Phase 2 wires the buttons)."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static

from aisquare.models import CheckStatus, DoctorCheck

_SYMBOL = {CheckStatus.ok: "✓", CheckStatus.warn: "⚠", CheckStatus.fail: "✗"}
_STYLE = {CheckStatus.ok: "green", CheckStatus.warn: "yellow", CheckStatus.fail: "bold red"}


def render_checks(checks: list[DoctorCheck]) -> Text:
    """The `aisquare doctor` output as rich text (fixes indented under their check)."""
    text = Text()
    for check in checks:
        text.append(f"{_SYMBOL[check.status]} {check.name}: ", style=_STYLE[check.status])
        text.append(f"{check.detail}\n")
        if check.fix and check.status is not CheckStatus.ok:
            text.append(f"    → {check.fix}\n", style="dim")
    if not checks:
        text.append("(no checks yet)", style="dim")
    return text


class DoctorView(VerticalScroll):
    """Scrollable doctor report for the selected project."""

    DEFAULT_CSS = """
    DoctorView { padding: 1 2; }
    """

    def compose(self) -> ComposeResult:
        yield Static("", id="doctor-report")

    def show(self, checks: list[DoctorCheck]) -> None:
        self.query_one("#doctor-report", Static).update(render_checks(checks))
