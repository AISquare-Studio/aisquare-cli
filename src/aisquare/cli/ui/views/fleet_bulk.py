"""Settings ▸ Fleet: *Restart all…*, *Stop all…*, *Pause all…* and *Resume all…*, and the
dialog each opens — the one a project card's *Pause agents* / *Resume agents* opens too.

User feedback (2026-10-19): with four managers running, a restart of all of them was
four views and four clicks, and the clicking around between panes had destroyed a
manager more than once. One gesture now does every running agent of every project, or
the managers alone. It is ``fleet_service.bulk``, the service function ``fleet restart
--all`` and ``fleet stop --all`` call.

:class:`ConfirmBulk` reads the plan when it opens (``fleet_service.bulk_plan``, off the
UI thread) and lists what it read, by project. Its *Managers only* switch redraws from
that read. Confirm hands back the plan it SHOWED, and ``bulk`` reads the fleet again
before it acts: for a stop, an agent that ended since is skipped as already exited; one
started since was never shown, so it is left alone. Cancel has the focus, and Escape
cancels.

A restart brings back the agents that died, not only the running ones (#38, after an
outside SIGTERM killed a whole fleet and Restart all restarted none): an agent killed,
crashed or left without its tmux server is listed apart, ``💤 exited (143) — will be
started again``, and the count says how many are running and how many dead. One aisquare
stopped on purpose is left, and says so.

*Pause* and *Resume* are ``fleet pause`` and ``fleet resume`` (``fleet_service.bulk``'s
``pause`` and ``resume``): a pause exits every running agent and keeps its claim, worktree
and session, and sets the project's ``fleet-paused`` signal; a resume clears it and starts
again exactly the agents a pause stopped. Opened for one project (:class:`BulkAsked`, from its
card's menu or its Settings tab), the dialog lists that project's agents alone, and the shell
runs it (``FleetApp.on_bulk_asked``) with the toasts :func:`agent_toast` and
:func:`report_toast` give Settings ▸ Fleet's.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, Label, Static, Switch
from textual.worker import Worker, WorkerState

from aisquare.cli.ui.click_guard import GuardedButton
from aisquare.cli.ui.sidebar import STATE_CHIP, agent_name, project_name
from aisquare.cli.ui.views.agent import AgentRestarted
from aisquare.models import FleetAgentStatus, ProjectInfo
from aisquare.services import fleet as fleet_service

BULK_WORKER = "fleet-bulk"
"""The restart, stop, pause or resume of many agents: one at a time, wherever it was asked
(Settings ▸ Fleet, a card's menu, a Settings tab), so each of them waits for it."""
PLAN_WORKER = "fleet-bulk-plan"
"""The dialog's read of what the action would act on."""

VERB: dict[fleet_service.BulkAction, str] = {
    "restart": "Restart",
    "stop": "Stop",
    "pause": "Pause",
    "resume": "Resume",
}
TITLE: dict[fleet_service.BulkAction, str] = {
    "restart": "Restart every running agent, and every one that died?",
    "stop": "Stop every running agent?",
    "pause": "Pause every running agent?",
    "resume": "Resume every paused agent?",
}
PROJECT_TITLE: dict[fleet_service.BulkAction, str] = {
    "pause": "Pause the running agents of {name}?",
    "resume": "Resume the paused agents of {name}?",
}
"""The title for one project (:class:`BulkAsked`); ``{name}`` is its display name."""
ABOUT: dict[fleet_service.BulkAction, str] = {
    "restart": "Each one is started again under its label, with the same role, task, "
    "worktree and account; a running one is stopped first. Its session is resumed from its "
    "transcript when that is on disk. An agent that died without being stopped (killed, "
    "crashed, its tmux server gone) is brought back too; one stopped on purpose is left as "
    "it is.",
    "stop": "Each one is sent /exit, given a grace period, and then its window is killed. "
    "Its claims go back to the board. Exited (💤) agents are left as they are.",
    "pause": "Each one is sent /exit as a stop does, and keeps its label, task claim, "
    "worktree, account and session for a resume. Its project is fleet-paused, so its manager "
    "spawns nothing. Exited (💤) agents are left as they are.",
    "resume": "Each paused agent is started again under its label, with the same role, task, "
    "worktree and account, its session resumed from its transcript when that is on disk; its "
    "project's fleet-paused signal is cleared. An agent that exited without a pause stays as "
    "it is.",
}
WILL_START = "will be started again"
"""A dead row a restart brings back (#38), after its state in the dialog's list."""
WILL_RESUME = "will be resumed"
"""A paused row a resume brings back, after its state in the dialog's list."""
WILL_END = "its pause will be ended, not resumed"
"""A paused row whose task closed meanwhile, which a resume stops for good (its claims go back
to the board) rather than resume: after its task's state in the dialog's list."""


class BulkAsked(Message):
    """*Pause agents* / *Resume agents* for one project — its card's menu, its Settings tab:
    the shell opens :class:`ConfirmBulk` for it and runs what was confirmed."""

    def __init__(self, action: fleet_service.BulkAction, project_id: str) -> None:
        super().__init__()
        self.action: fleet_service.BulkAction = action
        self.project_id = project_id


def applicable(
    agents: Sequence[FleetAgentStatus] | None, *, fleet_paused: bool = False
) -> dict[str, bool]:
    """Whether a pause and a resume would act on any of a project's ``agents``: a pause on one
    with a process, a resume on a paused one, or on the project's ``fleet-paused`` signal
    when it reads on (``fleet_paused``), which a resume clears: a signal-only pause from an
    earlier version, or one a ``--managers`` resume left on. Both, when the agents are not
    known (``None``: no frame yet, a collapsed group's member) — the dialog reads them and
    says."""
    if agents is None:
        return {"pause": True, "resume": True}
    return {
        "pause": any(status.state in fleet_service.PROCESS_STATES for status in agents),
        "resume": fleet_paused or any(status.state == "paused" for status in agents),
    }


def bulk_running(app: App[Any]) -> bool:
    """Whether a restart, stop, pause or resume of many agents runs anywhere in ``app``: one at
    a time, whoever started it (:data:`BULK_WORKER`)."""
    return any(worker.name == BULK_WORKER and not worker.is_finished for worker in app.workers)


def count_words(count: int, *, managers_only: bool, dead: int = 0) -> str:
    """``4 managers``, ``1 agent``, ``3 agents (2 running, 1 dead)``: what the confirm button
    names, with how many died when any did (#38)."""
    noun = "manager" if managers_only else "agent"
    words = f"{count} {noun}{'' if count == 1 else 's'}"
    return f"{words} ({count - dead} running, {dead} dead)" if dead else words


def plan_text(plan: fleet_service.BulkPlan) -> Text:
    """The dialog's list: each project's name, then ``label · role · state`` per agent.

    The rows the action leaves are listed dim with why (an exited agent, the session
    asq runs in); "nothing to restart" heads the list when no row is acted on. A resume
    that is not the owner's (asq run in an agent's window, ``BulkPlan.owners``) lists each
    paused row as refused, with the owner's refusal, never as one that will be resumed. Every
    name is data, appended as text and never as markup.
    """
    text = Text()
    if not plan.targets and plan.acts:  # a resume with no agent paused: the signal alone
        named = [project_name(p) for p in plan.projects if p.id in plan.fleet_paused]
        text.append("no agent is paused; Resume clears the fleet-paused signal of ", style="bold")
        text.append(", ".join(named) + "\n", style="bold")
    elif not plan.targets:
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
        elif row.refused is not None:
            line.append(f" — refused: {row.refused}")
            line.stylize("dim")
        elif plan.action == "resume" and row.closes is not None:
            line.append(f" — {row.closes}: {WILL_END}")
        elif plan.action == "resume":
            line.append(f" — {WILL_RESUME}")
        elif row.dead and plan.action == "restart":
            # Apart from the running ones (#38): its exit, and that it comes back.
            code = fleet_service.exit_code_of(row.status)
            exit_code = f" ({code})" if code is not None else ""
            line.append(f"{exit_code} — {WILL_START}")
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
    second click that reaches it before the dialog is painted is not a confirmation; one
    after it is, at once. ``project`` keeps it to that project's agents (a card's *Pause
    agents*), its title naming it.
    """

    CSS = """
    ConfirmBulk { align: center middle; }
    ConfirmBulk #bulk-box {
        width: 72; height: auto; max-height: 90%; border: heavy $error; background: $surface;
        padding: 1 2;
    }
    ConfirmBulk #bulk-box.restart, ConfirmBulk #bulk-box.pause, ConfirmBulk #bulk-box.resume {
        border: heavy $warning;
    }
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

    def __init__(
        self, action: fleet_service.BulkAction, project: ProjectInfo | None = None
    ) -> None:
        super().__init__()
        self.action: fleet_service.BulkAction = action
        self.project = project
        self.plan: fleet_service.BulkPlan | None = None
        """What the dialog read when it opened: every row, managers and teams alike."""
        self.failed: str | None = None

    def heading(self) -> str:
        """What the dialog asks: of every project, or of ``project`` by its name."""
        if self.project is not None and self.action in PROJECT_TITLE:
            return PROJECT_TITLE[self.action].format(name=project_name(self.project))
        return TITLE[self.action]

    def compose(self) -> ComposeResult:
        verb = VERB[self.action]
        with Vertical(id="bulk-box", classes=self.action):
            yield Static(Text(self.heading()), id="bulk-title")
            yield Static(Text(ABOUT[self.action]), id="bulk-about")
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
        scope = [self.project] if self.project is not None else None
        self.run_worker(
            lambda: fleet_service.bulk_plan(action, projects=scope),
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
        if count == 0 and plan.acts:  # a resume of the signal alone
            confirm.label = "Clear fleet-paused"
            confirm.refresh(layout=True)
            confirm.disabled = False
            return
        # Running and dead apart for a restart (#38); every agent a resume acts on is paused.
        dead = sum(1 for row in plan.targets if row.dead) if self.action == "restart" else 0
        verb = VERB[self.action]
        confirm.label = (
            f"{verb} {count_words(count, managers_only=plan.managers_only, dead=dead)}"
            if count
            else verb
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
        if plan is not None and plan.acts:
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


def agent_toast(node: Widget | App[Any], receipt: fleet_service.BulkReceipt, action: str) -> None:
    """One agent's toast, as it is done, from ``node``; a skip is named in the summary instead.

    A restart or a resume is told as the agent view's own restart is, through
    :class:`AgentRestarted`: the shell says it and moves the view to the new row only from the
    replaced row's view, still on screen. From Settings ▸ Fleet or a card's menu, that is
    never, unless the user went there meanwhile.
    """
    result = receipt.result
    if receipt.outcome == "failed":
        node.notify(
            f"could not {action} {named(receipt)}: {receipt.why}",
            severity="error",
            timeout=8,
            markup=False,
        )
    elif isinstance(result, fleet_service.RestartReceipt):
        node.post_message(AgentRestarted(result))
    elif isinstance(result, fleet_service.StopReceipt):
        said = "⏸ paused" if action == "pause" else "✓ stopped"
        node.notify(f"{said} {named(receipt)}", timeout=5, markup=False)
        if result.release_failed:
            node.notify(
                f"claims: {result.release_failed}", severity="warning", timeout=8, markup=False
            )


def report_toast(node: Widget | App[Any], event: Worker.StateChanged, action: str) -> None:
    """The summing-up toast once a :data:`BULK_WORKER` is done, from ``node``: the report's
    summary (:func:`summary_text`), or why it could not run (a pause signal that could not be
    set refuses it before any agent is touched)."""
    report = event.worker.result
    if event.state is WorkerState.SUCCESS and isinstance(report, fleet_service.BulkReport):
        failed = bool(report.of("failed") or report.unread)
        node.notify(
            summary_text(report),
            severity="error" if failed else ("information" if report.ok else "warning"),
            timeout=12 if not report.ok or report.of("skipped") else 6,
            markup=False,
        )
    elif event.state is WorkerState.ERROR:
        node.notify(
            f"could not {action} the agents: {event.worker.error}",
            severity="error",
            timeout=8,
            markup=False,
        )


def summary_text(report: fleet_service.BulkReport) -> str:
    """``restarted 4 · skipped 1 · failed 0``, then who was skipped and who failed, and why."""
    parts = [report.summary()]
    if report.signals:
        word = "fleet-paused" if report.action == "pause" else "no longer fleet-paused"
        parts.append(f"{word}: " + ", ".join(project_name(project) for project in report.signals))
    if report.kept:
        kept = [f"{project_name(project)} ({left} still paused)" for project, left in report.kept]
        parts.append("still fleet-paused: " + ", ".join(kept))
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
