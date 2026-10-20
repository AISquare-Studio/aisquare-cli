"""Settings ▸ Fleet: *Restart all…* and *Stop all…*, and the dialog each opens.

User feedback (2026-10-19): with four managers running, a restart of all of them was
four views and four clicks, and the clicking around between panes had destroyed a
manager more than once. One gesture now does every running agent of every project, or
the managers alone. It is ``fleet_service.bulk``, the service function ``fleet restart
--all`` and ``fleet stop --all`` call.

:class:`ConfirmBulk` reads the plan when it opens (``fleet_service.bulk_plan``, off the
UI thread) and lists what it read, by project. Its *Managers only* switch redraws from
that read. Confirm hands back the plan it SHOWED, and ``bulk`` reads the fleet again
before it acts: an agent that ended since is skipped as already exited, and one started
since was never shown, so it is left alone. Cancel has the focus, and Escape cancels.
"""

from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static, Switch
from textual.worker import Worker, WorkerState

from aisquare.cli.ui.click_guard import GuardedButton
from aisquare.cli.ui.sidebar import STATE_CHIP, agent_name, project_name
from aisquare.services import fleet as fleet_service

BULK_WORKER = "fleet-bulk"
"""The restart or stop of every agent: one at a time, so the Settings buttons wait for it."""
PLAN_WORKER = "fleet-bulk-plan"
"""The dialog's read of what the action would act on."""

VERB: dict[fleet_service.BulkAction, str] = {"restart": "Restart", "stop": "Stop"}
ABOUT: dict[fleet_service.BulkAction, str] = {
    "restart": "Each one is stopped and started again under its label, with the same role, "
    "task, worktree and account. Its session is resumed from its transcript when that is "
    "on disk.",
    "stop": "Each one is sent /exit, given a grace period, and then its window is killed. "
    "Its claims go back to the board.",
}


def count_words(count: int, *, managers_only: bool) -> str:
    """``4 managers``, ``1 agent``: what the confirm button names."""
    noun = "manager" if managers_only else "agent"
    return f"{count} {noun}{'' if count == 1 else 's'}"


def plan_text(plan: fleet_service.BulkPlan) -> Text:
    """The dialog's list: each project's name, then ``label · role · state`` per agent.

    The rows the action leaves are listed dim with why (an exited agent, the session
    asq runs in); "nothing to restart" heads the list when no row is acted on. Every
    name is data, appended as text and never as markup.
    """
    text = Text()
    if not plan.targets:
        text.append(f"nothing to {plan.action}\n", style="bold")
    shown: str | None = None
    for row in plan.rows:
        if row.project.id != shown:
            shown = row.project.id
            text.append(("\n" if text else "") + project_name(row.project) + "\n", style="bold")
        agent, state = row.agent, row.status.state
        chip, chip_style = STATE_CHIP.get(state, ("·", "dim"))
        line = Text("  ")
        line.append(f"{agent.label} · {agent.role} · ")
        line.append(f"{chip} {state}", style=chip_style)
        if row.skip is not None:
            line.append(f" — left: {row.skip}")
            line.stylize("dim")
        text.append_text(line)
        text.append("\n")
    for project, why in plan.unread:
        text.append(f"\n⚠ {project_name(project)}: its agents could not be read — {why}\n")
    text.rstrip()
    return text


class ConfirmBulk(ModalScreen[fleet_service.BulkPlan | None]):
    """ "Restart every running agent?" — the list, a *Managers only* switch, Confirm and Cancel.

    Answers the plan it showed when confirmed, ``None`` on Cancel or Escape. Confirm is
    greyed until the plan is read, and stays greyed when it acts on nobody. It is a
    :class:`GuardedButton`: the dialog opens where *Restart all…* was clicked, and a
    second click landing on it as it appears is not a confirmation.
    """

    CSS = """
    ConfirmBulk { align: center middle; }
    ConfirmBulk #bulk-box {
        width: 72; height: auto; max-height: 90%; border: heavy $error; background: $surface;
        padding: 1 2;
    }
    ConfirmBulk #bulk-box.restart { border: heavy $warning; }
    ConfirmBulk #bulk-title { height: auto; text-style: bold; }
    ConfirmBulk #bulk-about { height: auto; color: $text-muted; margin-bottom: 1; }
    ConfirmBulk #bulk-switch-row { height: auto; }
    ConfirmBulk #bulk-switch-row Label { padding: 1 0 0 1; }
    ConfirmBulk #bulk-scroll { height: auto; max-height: 20; margin-top: 1; }
    ConfirmBulk #bulk-list { height: auto; }
    ConfirmBulk #bulk-buttons { height: auto; margin-top: 1; align: right middle; }
    ConfirmBulk #bulk-buttons Button { margin-left: 2; }
    """
    BINDINGS: ClassVar = [("escape", "cancel_bulk", "cancel")]

    def __init__(self, action: fleet_service.BulkAction) -> None:
        super().__init__()
        self.action: fleet_service.BulkAction = action
        self.plan: fleet_service.BulkPlan | None = None
        """What the dialog read when it opened: every row, managers and teams alike."""
        self.failed: str | None = None

    def compose(self) -> ComposeResult:
        verb = VERB[self.action]
        with Vertical(id="bulk-box", classes=self.action):
            yield Static(Text(f"{verb} every running agent?"), id="bulk-title")
            yield Static(
                Text(f"{ABOUT[self.action]} Exited (💤) agents are left as they are."),
                id="bulk-about",
            )
            with Horizontal(id="bulk-switch-row"):
                yield Switch(False, id="bulk-managers")
                yield Label("Managers only")
            with VerticalScroll(id="bulk-scroll"):
                yield Static(Text("reading the fleet…", style="dim"), id="bulk-list")
            with Horizontal(id="bulk-buttons"):
                yield GuardedButton(
                    verb,
                    id="bulk-confirm",
                    variant="error" if self.action == "stop" else "warning",
                    disabled=True,
                )
                yield Button("Cancel", id="bulk-cancel")

    def on_mount(self) -> None:
        self.query_one("#bulk-cancel", Button).focus()
        action = self.action
        self.run_worker(
            lambda: fleet_service.bulk_plan(action),
            name=PLAN_WORKER,
            group=PLAN_WORKER,
            exclusive=True,
            thread=True,
            exit_on_error=False,
        )

    @on(Worker.StateChanged)
    def _plan_read(self, event: Worker.StateChanged) -> None:
        if event.worker.name != PLAN_WORKER:
            return
        if event.state is WorkerState.SUCCESS and isinstance(
            event.worker.result, fleet_service.BulkPlan
        ):
            self.plan = event.worker.result
        elif event.state is WorkerState.ERROR:
            self.failed = f"could not read the fleet: {event.worker.error}"
        else:
            return
        self._redraw()

    @property
    def managers_only(self) -> bool:
        return self.query_one("#bulk-managers", Switch).value

    def shown(self) -> fleet_service.BulkPlan | None:
        """The plan as the list draws it now: the managers' rows alone with the switch on."""
        if self.plan is None:
            return None
        return self.plan.managers() if self.managers_only else self.plan

    @on(Switch.Changed, "#bulk-managers")
    def _switched(self, event: Switch.Changed) -> None:
        event.stop()
        self._redraw()

    def _redraw(self) -> None:
        listing = self.query_one("#bulk-list", Static)
        confirm = self.query_one("#bulk-confirm", Button)
        plan = self.shown()
        if plan is None:
            listing.update(Text(self.failed or "reading the fleet…", style="dim"))
            confirm.disabled = True
            return
        listing.update(plan_text(plan))
        count = len(plan.targets)
        verb = VERB[self.action]
        confirm.label = (
            f"{verb} {count_words(count, managers_only=plan.managers_only)}" if count else verb
        )
        # A new label is not a layout change in Textual, so the button kept the width it was
        # laid out at, for "Restart" while the plan was read: "Restart 2 managers" read
        # "Restart 2" (tester-pr266-ui, real UI).
        confirm.refresh(layout=True)
        confirm.disabled = count == 0

    @on(Button.Pressed, "#bulk-confirm")
    def _confirm(self, event: Button.Pressed) -> None:
        event.stop()
        plan = self.shown()
        if plan is not None and plan.targets:
            self.dismiss(plan)

    @on(Button.Pressed, "#bulk-cancel")
    def _cancel(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(None)

    def action_cancel_bulk(self) -> None:
        self.dismiss(None)


def named(receipt: fleet_service.BulkReceipt) -> str:
    """``coder-1 (alpha)``: the receipt's agent, as every toast names one (``agent_name``)."""
    return agent_name(receipt.agent.label, receipt.project)


def summary_text(report: fleet_service.BulkReport) -> str:
    """``restarted 4 · skipped 1 · failed 0``, then who was skipped and who failed, and why."""
    parts = [report.summary()]
    skipped = [f"{named(r)} — {r.why}" for r in report.of("skipped")]
    failed = [f"{named(r)} — {r.why}" for r in report.of("failed")]
    unread = [f"{project_name(project)} — {why}" for project, why in report.unread]
    if skipped:
        parts.append("skipped: " + "; ".join(skipped))
    if failed:
        parts.append("failed: " + "; ".join(failed))
    if unread:
        parts.append("not read: " + "; ".join(unread))
    return "\n".join(parts)
