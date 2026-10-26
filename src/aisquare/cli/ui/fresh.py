"""Start fresh… — on a project card's menu and on its Settings tab — and the dialogs it opens.

``services.fresh`` does the work, as ``aisquare project fresh`` has it done: this asks first
and hands the answer on. :class:`ConfirmFresh` reads the plan when it opens
(``services.fresh.plan``, off the UI thread), with every extra counted, and shows what is
always cleared, the extras ticked, and that a new blank manager starts. Its three
checkboxes (*Project memory*, *Prompt log & metrics*, *Fleet git branches*), all off,
redraw from that one read (:func:`narrowed`). A plan the service refuses (a board another
project's fleet reads, a call from inside the project's own fleet) says why, and *Start
fresh* stays greyed. Cancel has the focus, and Escape cancels.

When the plan finds work no remote has in a tree or branch it would remove,
:class:`ConfirmDiscard` lists each one and asks again; cancelling there goes back to the
first dialog, nothing done. The answer is a :class:`FreshChoice`, which the shell
(``FleetApp``) runs ``services.fresh.start_fresh`` with in a thread worker: the run plans
again before it acts, and refuses work found since that nobody agreed to discard.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Static
from textual.worker import Worker, WorkerState

from aisquare.cli.ui.click_guard import GuardedButton
from aisquare.models import FreshPlan, FreshUnpushed, ProjectInfo
from aisquare.services import fresh as fresh_service
from aisquare.services.project import display_name

PLAN_WORKER = "fresh-plan"
"""The dialog's read of what Start fresh would clear (``services.fresh.plan``)."""

EXTRAS: tuple[tuple[str, str], ...] = (
    ("fresh-memory", "Project memory"),
    ("fresh-prompts", "Prompt log & metrics"),
    ("fresh-branches", "Fleet git branches"),
)
"""The dialog's checkboxes, by id, in the order shown: ``--memory``, ``--prompts``,
``--branches``."""

ALWAYS = 6
"""How many of ``services.fresh.count_words``'s words are what is always cleared: tasks,
events, sessions, signals and marks, fleet agents and worktrees, never ``None``."""


class StartFresh(Message):
    """Start fresh… was asked for a project — its card's menu, its Settings tab: the shell
    opens :class:`ConfirmFresh` for it."""

    def __init__(self, project_id: str) -> None:
        super().__init__()
        self.project_id = project_id


@dataclass(frozen=True)
class FreshChoice:
    """What :class:`ConfirmFresh` was confirmed with: ``start_fresh``'s extras, and whether
    the unpushed work it listed may go."""

    project_id: str
    memory: bool = False
    prompts: bool = False
    branches: bool = False
    discard_unpushed: bool = False


def narrowed(plan: FreshPlan, *, memory: bool, prompts: bool, branches: bool) -> FreshPlan:
    """``plan``, read with every extra, as the plan with only these: the counts of an extra
    not chosen are not counted, the branches not deleted, and the unpushed work found only
    on a branch no tree has out is not at risk."""
    counts = plan.counts.model_copy(
        update={
            "entries": plan.counts.entries if memory else None,
            "prompts": plan.counts.prompts if prompts else None,
            "metrics": plan.counts.metrics if prompts else None,
            "branches": plan.counts.branches if branches else None,
        }
    )
    return plan.model_copy(
        update={
            "memory": memory,
            "prompts": prompts,
            "branches": branches,
            "counts": counts,
            "fleet_branches": plan.fleet_branches if branches else {},
            "unpushed": [item for item in plan.unpushed if branches or item.path is not None],
        }
    )


def plan_text(plan: FreshPlan) -> Text:
    """What the dialog says the plan does: always, the extras chosen, then the new manager."""
    words = fresh_service.count_words(plan.counts)
    always, also = words[:ALWAYS], words[ALWAYS:]  # an extra not chosen is not counted
    text = Text()
    text.append("Always cleared: ", style="bold")
    text.append(" · ".join(always))
    if plan.live_agents:
        text.append(f"\nStopped first: {', '.join(plan.live_agents)}")
    if plan.sessions:
        text.append(f"\nIts tmux session is killed: {', '.join(plan.sessions)}")
    if also:
        text.append("\nAlso cleared: ", style="bold")
        text.append(" · ".join(also))
    text.append("\nThen a new blank manager starts.")
    if plan.unpushed:
        count = len(plan.unpushed)
        places = "worktree or branch holds" if count == 1 else "worktrees or branches hold"
        text.append(
            f"\n⚠ {count} {places} work no remote has — you are asked again before it is "
            "thrown away.",
            style="bold",
        )
    return text


def unpushed_row(item: FreshUnpushed) -> str:
    """``fleet/x/coder-1 — 2 commits on no remote · uncommitted: yes — /path``: one line of
    :class:`ConfirmDiscard`'s list."""
    branch = item.branch or "(detached)"
    commits = f"{item.commits} commit{'s' if item.commits != 1 else ''} on no remote"
    where = f" — {item.path}" if item.path is not None else " — no worktree"
    return f"{branch} — {commits} · uncommitted: {'yes' if item.uncommitted else 'no'}{where}"


class ConfirmFresh(ModalScreen[FreshChoice | None]):
    """ "Start <name> fresh?" — the plan, three extras, *Start fresh* and Cancel.

    Answers a :class:`FreshChoice` when confirmed (through :class:`ConfirmDiscard` when the
    plan finds unpushed work), ``None`` on Cancel or Escape. *Start fresh* is greyed until
    the plan is read, and for good when it is refused. It is a :class:`GuardedButton`: the
    dialog opens where *Start fresh…* was clicked.
    """

    CSS = """
    ConfirmFresh { align: center middle; }
    ConfirmFresh #fresh-box {
        width: 76; height: auto; max-height: 100%; border: heavy $error; background: $surface;
        padding: 1 2;
    }
    ConfirmFresh #fresh-title { height: auto; text-style: bold; }
    ConfirmFresh #fresh-about { height: auto; color: $text-muted; margin-bottom: 1; }
    ConfirmFresh #fresh-scroll { height: auto; max-height: 10; }
    ConfirmFresh #fresh-plan { height: auto; }
    ConfirmFresh #fresh-extras { height: auto; margin-top: 1; }
    ConfirmFresh #fresh-extras Checkbox { border: none; height: 1; padding: 0 1; }
    ConfirmFresh #fresh-extras Checkbox:focus { text-style: bold reverse; }
    ConfirmFresh #fresh-buttons { height: auto; margin-top: 1; align: right middle; }
    ConfirmFresh #fresh-buttons Button { margin-left: 2; }
    """
    BINDINGS: ClassVar = [("escape", "cancel_fresh", "cancel")]

    def __init__(self, project: ProjectInfo) -> None:
        super().__init__()
        self.project = project
        self.plan: FreshPlan | None = None
        """What the dialog read when it opened, every extra counted."""
        self.refused: str | None = None
        """Why the plan was refused, or could not be read: shown, and nothing to confirm."""

    def compose(self) -> ComposeResult:
        with Vertical(id="fresh-box"):
            yield Static(Text(f"Start {display_name(self.project)} fresh?"), id="fresh-title")
            yield Static(
                Text(
                    "Its agents are stopped, their worktrees removed, and its board and fleet "
                    "cleared: task numbers start at #1 again. The folder, its other branches, "
                    "its settings and its snapshot are kept."
                ),
                id="fresh-about",
            )
            with VerticalScroll(id="fresh-scroll"):
                yield Static(Text("reading the project…", style="dim"), id="fresh-plan")
            with Vertical(id="fresh-extras"):
                for checkbox, label in EXTRAS:
                    yield Checkbox(label, False, id=checkbox)
            with Horizontal(id="fresh-buttons"):
                yield GuardedButton(
                    "Start fresh", id="fresh-confirm", variant="error", disabled=True
                )
                yield Button("Cancel", id="fresh-cancel")

    def on_mount(self) -> None:
        self.query_one("#fresh-cancel", Button).focus()
        project = self.project
        self.run_worker(
            lambda: fresh_service.plan(project, memory=True, prompts=True, branches=True),
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
        if event.state is WorkerState.SUCCESS and isinstance(event.worker.result, FreshPlan):
            self.plan = event.worker.result
        elif event.state is WorkerState.ERROR:
            error = event.worker.error
            self.refused = (
                str(error)
                if isinstance(error, fresh_service.FreshRefused)
                else f"could not read what it would clear: {type(error).__name__}: {error}"
            )
        else:
            return
        self._redraw()

    def extras(self) -> tuple[bool, bool, bool]:
        """The checkboxes as they stand: memory, prompts, branches."""
        memory, prompts, branches = (
            self.query_one(f"#{checkbox}", Checkbox).value for checkbox, _ in EXTRAS
        )
        return memory, prompts, branches

    def shown(self) -> FreshPlan | None:
        """The plan as the dialog draws it now, for the extras ticked."""
        if self.plan is None or self.refused is not None:
            return None
        memory, prompts, branches = self.extras()
        return narrowed(self.plan, memory=memory, prompts=prompts, branches=branches)

    @on(Checkbox.Changed)
    def _ticked(self, event: Checkbox.Changed) -> None:
        event.stop()
        self._redraw()

    def _redraw(self) -> None:
        listing = self.query_one("#fresh-plan", Static)
        confirm = self.query_one("#fresh-confirm", Button)
        plan = self.shown()
        if self.refused is not None:
            listing.update(Text(self.refused, style="bold red"))
        elif plan is None:
            listing.update(Text("reading the project…", style="dim"))
        else:
            listing.update(plan_text(plan))
        confirm.disabled = plan is None

    @on(Button.Pressed, "#fresh-confirm")
    def _confirm(self, event: Button.Pressed) -> None:
        event.stop()
        plan = self.shown()
        if plan is None:
            return
        memory, prompts, branches = self.extras()
        choice = FreshChoice(self.project.id, memory=memory, prompts=prompts, branches=branches)
        if not plan.unpushed:
            self.dismiss(choice)
            return

        def discard(yes: bool | None) -> None:
            if yes:
                self.dismiss(
                    FreshChoice(
                        choice.project_id,
                        memory=memory,
                        prompts=prompts,
                        branches=branches,
                        discard_unpushed=True,
                    )
                )

        self.app.push_screen(ConfirmDiscard(self.project, plan.unpushed), discard)

    @on(Button.Pressed, "#fresh-cancel")
    def _cancel(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(None)

    def action_cancel_fresh(self) -> None:
        self.dismiss(None)


class ConfirmDiscard(ModalScreen[bool]):
    """ "Throw away the work no remote has?" — each tree or branch, *Discard and start fresh*
    and Cancel. Answers ``True`` for the first; Cancel or Escape ``False``. Cancel has the
    focus."""

    CSS = """
    ConfirmDiscard { align: center middle; }
    ConfirmDiscard #discard-box {
        width: 84; height: auto; max-height: 90%; border: heavy $error; background: $surface;
        padding: 1 2;
    }
    ConfirmDiscard #discard-title { height: auto; text-style: bold; }
    ConfirmDiscard #discard-scroll { height: auto; max-height: 14; margin-top: 1; }
    ConfirmDiscard #discard-list { height: auto; }
    ConfirmDiscard #discard-buttons { height: auto; margin-top: 1; align: right middle; }
    ConfirmDiscard #discard-buttons Button { margin-left: 2; }
    """
    BINDINGS: ClassVar = [("escape", "cancel_discard", "cancel")]

    def __init__(self, project: ProjectInfo, unpushed: list[FreshUnpushed]) -> None:
        super().__init__()
        self.project = project
        self.unpushed = unpushed

    def compose(self) -> ComposeResult:
        count = len(self.unpushed)
        with Vertical(id="discard-box"):
            yield Static(
                Text(
                    f"{display_name(self.project)}'s fleet holds work no remote has, in "
                    f"{count} place{'s' if count != 1 else ''}. Throw it away?"
                ),
                id="discard-title",
            )
            with VerticalScroll(id="discard-scroll"):
                yield Static(
                    Text("\n".join(unpushed_row(item) for item in self.unpushed)),
                    id="discard-list",
                )
            with Horizontal(id="discard-buttons"):
                yield GuardedButton(
                    "Discard and start fresh", id="discard-confirm", variant="error"
                )
                yield Button("Cancel", id="discard-cancel")

    def on_mount(self) -> None:
        self.query_one("#discard-cancel", Button).focus()

    @on(Button.Pressed, "#discard-confirm")
    def _yes(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(True)

    @on(Button.Pressed, "#discard-cancel")
    def _no(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(False)

    def action_cancel_discard(self) -> None:
        self.dismiss(False)
