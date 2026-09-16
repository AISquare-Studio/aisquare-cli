"""The Spawn dialog — the sidebar's spawn-agent row, over ``services.fleet.spawn``.

docs/plans/spawn-personas.md §4.1 (the field table this follows row by row) and
docs/plans/fleet-tui.md §4.1, §5.7 (the row, the label rules, the 🎲 form). The
dialog is a form in front of the one spawn the CLI runs: every field is one
keyword of :func:`aisquare.services.fleet.spawn`, and a field still showing what
it opened with is sent as ``None`` — "the role's default", exactly what an
omitted CLI flag means — so the service resolves the default at spawn time from
the config it reads then, not from the one this form read when it opened. The
prefilled label follows the same rule: sent only when the user changed it, so
the service picks the free label under the store it is writing with.

**Two steps: who runs it, then as whom** (§4.1, P4). The target fields come
first — Role (with *Pick…*), Account, Binary — and the Persona select right
after them, with the persona's description under it. It preselects the role's
``[fleet.roles.<role>].persona`` and follows the role until the user picks one.
``persona=`` is sent like every other field: ``None`` while the form shows the
role's default, the name once one is chosen, and ``""`` for an explicit *(none)*
over a role that has a default — ``spawn`` reads an empty name as "no persona",
so that choice is honoured rather than replaced by the config.

**Presets** — ``SpawnDialog(project, persona=, role=, binary=, account=)`` — are
applied at compose, never by poking widgets after mount: the persona-first flow
(the target picker, P7) opens the dialog already filled in. A preset is a
choice, and is sent. A preset role the list does not name (a numbered seat,
``coder2``) and a preset account the accounts read has not produced yet are
added as options, so they show on open. *Pick…* posts
:class:`PickTargetRequested`, which the dialog answers itself: the target picker
(``cli/ui/attach.py``) in "new" order, whose choice fills Role, Binary and Account
through their own change handlers — an open form keeps what was typed — and whose
*+ New account* hands over to the Accounts page. *Import…* beside the Persona
select opens the Personas tab's import dialog and selects what it imports.

Two things are sent although nobody touched them, because the default cannot
stand: in a project that is not a git repository the worktree switch is off and
disabled, so a role whose default IS a worktree is sent ``worktree=False``
rather than a ``None`` the service would refuse; and the manager's label is
always ``manager``, so the Label field is locked for that role.

The spawn runs in a thread worker (tmux, git and the first-prompt wait take
seconds) with ``exit_on_error=False``, the Manager tab's pattern: a
``FleetError`` is the service's answer and lands in the status line while the
dialog stays open; anything else lands there too, with its class name — never
a crash of the app. A spawn cannot be taken back once it has started, so while
one runs *Cancel* and ``Esc`` wait for its answer rather than pretend to cancel.

Keys: ``Esc`` cancels; ``Tab``/``Shift+Tab`` move; the buttons are the only
submit. Nothing else is bound — the modal owns focus while it is open, and the
agent pane behind it keeps every key it had.
"""

from __future__ import annotations

import random
import shlex
from collections.abc import Callable, Iterable
from typing import Any, ClassVar

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList, Select, Static, Switch, TextArea
from textual.worker import Worker, WorkerState

from aisquare.cli.ui.attach import AttachTargetScreen, NewAccountRequested, Target
from aisquare.cli.ui.persona_dialogs import ImportPersonaScreen
from aisquare.cli.ui.views.settings import permission_options
from aisquare.core import codenames, harness, personas
from aisquare.core.config import FleetRoleSettings, load_config
from aisquare.core.personas import Persona
from aisquare.core.store import store_session
from aisquare.core.workspace import git_common_root
from aisquare.models import AccountsOverview, ClaudeAccountStatus, ProjectInfo, TeamTask
from aisquare.services import claude_accounts as accounts_service
from aisquare.services import fleet as fleet_service
from aisquare.services import personas as personas_service

SPAWN_WORKER = "spawn-agent"
"""The worker that runs the spawn. Not ``spawn-manager`` — that is the Manager
tab's, and a worker's state change bubbles up to the app past both."""

ACCOUNTS_WORKER = "spawn-accounts"
"""The worker that reads the Claude accounts, so the dialog opens before they are read."""

NO_TASK = ""
"""The Task field's ``(none)``."""

THIS_SHELL = ""
"""The Account field's ``(this shell's)``: no ``--account`` at all."""

NO_PERSONA = ""
"""The Persona field's ``(none)`` — and what ``spawn`` reads as "no persona"."""

OPEN_TASK_STATUSES = ("todo", "doing", "review", "blocked")
"""A task an agent can still be spawned for; ``done`` and ``dropped`` are refused by the service."""

LABEL_MAX = 24
"""The longest label ``fleet_service.LABEL`` accepts."""

LABEL_RULE = (
    "a label is 2 to 24 characters: a lowercase letter, then lowercase letters, "
    "digits or '-' — no '.', ':' or spaces"
)


class PickTargetRequested(Message):
    """Choose who runs the new agent — a bind or an account — in the target picker (§4.6)."""

    def __init__(self, project_id: str) -> None:
        self.project_id = project_id
        super().__init__()


class SpawnCompleted(Message):
    """A Spawn dialog opened away from the sidebar row closed with a receipt."""

    def __init__(self, receipt: fleet_service.SpawnReceipt) -> None:
        self.receipt = receipt
        super().__init__()


def role_choices(bound: Iterable[str]) -> list[str]:
    """The fleet's roles in their fixed order, then every other role ``team bind`` knows, sorted."""
    return [
        *fleet_service.FLEET_ROLES,
        *sorted(set(bound) - set(fleet_service.FLEET_ROLES)),
    ]


def dice_label(role: str, rng: random.Random | None = None) -> str | None:
    """``<role>-<adjective>-<animal>`` from the codename lists; ``None`` when no pair fits.

    The pick is made among the words that fit, not clipped afterwards — a
    clipped word is not a word — and the longest words do not always fit:
    ``ui-tester-<7 letters>-<7 letters>`` is 25 characters, one past the limit.
    A role too long for any pair gets ``None`` so the caller can say so instead
    of offering a label the service would refuse.
    """
    pick = rng or random.Random()
    room = LABEL_MAX - len(role) - 2  # the two '-' separators
    shortest_animal = min(len(animal) for animal in codenames.ANIMALS)
    adjectives = [word for word in codenames.ADJECTIVES if len(word) + shortest_animal <= room]
    if not adjectives:
        return None
    adjective = pick.choice(adjectives)
    animal = pick.choice([word for word in codenames.ANIMALS if len(adjective) + len(word) <= room])
    label = f"{role}-{adjective}-{animal}"
    return label if fleet_service.is_label(label) else None


def split_agent_args(text: str) -> list[str]:
    """The Extra agent args field as ``agent_args``; ``ValueError`` on a quoting error."""
    return shlex.split(text)


def task_choice(task: TeamTask) -> str:
    """``<short id> [status] <title>`` — the short id is the one a label and a branch use."""
    return (
        f"{task.id.removeprefix('tsk_')[: fleet_service.TASK_SHORT]} [{task.status}] {task.title}"
    )


def account_choice(status: ClaudeAccountStatus) -> str:
    """``2 · account 2 · me@example.com`` — who a slot is, as the Accounts page names it."""
    who = status.identity.email if status.identity is not None else "not signed in"
    return f"{status.account.slot} · {status.label} · {who}"


def persona_choice(persona: Persona) -> str:
    """``skeptic · bundled`` — the name, and the layer it comes from."""
    return f"{persona.name} · {persona.layer}"


def _bound_roles() -> list[str]:
    """Roles named in ``team.profiles``; none when the config cannot be read (fail-open)."""
    try:
        return list(load_config().team.profiles)
    except Exception:  # a broken config costs the extra roles, never the dialog
        return []


class SpawnDialog(ModalScreen[fleet_service.SpawnReceipt | None]):
    """Spawn one agent for ``project``; dismisses with the receipt, or ``None`` on cancel."""

    DEFAULT_CSS = """
    SpawnDialog { align: center middle; }
    SpawnDialog #spawn-box { width: 90; max-width: 96%; height: 92%; border: heavy $accent;
                             background: $surface; padding: 0 1; }
    SpawnDialog #spawn-header { height: auto; padding: 1 0; }
    SpawnDialog #spawn-fields { height: 1fr; }
    SpawnDialog .spawn-row { height: auto; }
    SpawnDialog .spawn-row > Label { width: 18; padding-top: 1; }
    SpawnDialog .spawn-row > Select { width: 1fr; }
    SpawnDialog .spawn-row > Input { width: 1fr; }
    SpawnDialog #spawn-dice { min-width: 7; width: 7; }
    SpawnDialog #spawn-pick { min-width: 10; width: 10; }
    SpawnDialog #spawn-import { min-width: 12; width: 12; }
    SpawnDialog .spawn-note { height: auto; padding-left: 18; color: $text-muted; }
    SpawnDialog #spawn-worktree-note { padding: 1 0 0 1; height: auto; color: $text-muted; }
    SpawnDialog #spawn-prompt { height: 6; width: 1fr; }
    SpawnDialog #spawn-status { height: auto; padding: 0 0 0 0; }
    SpawnDialog #spawn-buttons { height: auto; align-horizontal: right; padding-bottom: 1; }
    SpawnDialog #spawn-buttons Button { margin-left: 2; }
    """
    BINDINGS: ClassVar = [Binding("escape", "cancel", "cancel")]

    def __init__(
        self,
        project: ProjectInfo,
        *,
        persona: str | None = None,
        role: str | None = None,
        binary: str | None = None,
        account: str | None = None,
        accounts: Callable[[], AccountsOverview] | None = accounts_service.overview,
    ) -> None:
        super().__init__()
        self.project = project
        self._accounts = accounts
        self._fleet = fleet_service.settings()
        self._git = fleet_service.is_git_project(project.root)
        self._role = role or "coder"
        roles = role_choices(_bound_roles())
        self._roles = roles if self._role in roles else [*roles, self._role]
        self._preset_binary = binary or ""
        self._preset_account = account
        self._account_statuses: list[ClaudeAccountStatus] = []
        self._personas, self._personas_unavailable = self._read_personas()
        self._persona_touched = persona is not None
        """A preset or a pick: from then on the persona no longer follows the role."""
        self._persona_shown = persona if persona is not None else self._persona_default(self._role)
        """The persona value the form itself last put in the field; any other is the user's."""
        self._tasks, self._tasks_unavailable = self._read_tasks()
        self._manager_live = self._read_manager_live()
        self._prefill = self._label_for(self._role, NO_TASK)
        """The label the form filled in for the current role and task; equal means untouched."""
        self._kept_label: str | None = None
        """What the user had typed, kept while the manager role locks the field."""
        self._spawning = False

    # --- what the form reads when it opens ----------------------------------------------

    def _read_tasks(self) -> tuple[list[TeamTask], str | None]:
        try:
            with store_session() as store:
                tasks = store.team_tasks(self.project.id)
        except Exception as exc:  # the store is busy: no task list, still a dialog
            return [], f"tasks unavailable — {type(exc).__name__}: {exc}"
        return [task for task in tasks if task.status in OPEN_TASK_STATUSES], None

    def _read_personas(self) -> tuple[list[Persona], str | None]:
        """The personas ``spawn`` accepts here: ``catalogue(project.root)``, winners only."""
        try:
            found, _invalid = personas.catalogue(self.project.root)
        except Exception as exc:  # a layer we cannot read costs the list, never the dialog
            return [], f"personas unavailable — {type(exc).__name__}: {exc}"
        return found, None

    def _read_manager_live(self) -> bool:
        try:
            return fleet_service.manager_of(self.project) is not None
        except Exception:  # unknown: offer the role; the service refuses a second manager itself
            return False

    def _defaults(self, role: str) -> FleetRoleSettings:
        return fleet_service.role_settings(role, self._fleet)

    def _persona_default(self, role: str) -> str:
        return self._defaults(role).persona or NO_PERSONA

    def _label_for(self, role: str, task_id: str) -> str:
        """The prefill: ``<role>-<task short id>`` or ``<role>-<n>``, free among live agents."""
        try:
            return fleet_service.next_label(self.project, role, task_id=task_id or None)
        except Exception:  # no store to ask: the service re-picks at spawn anyway
            if role == "manager":
                return fleet_service.MANAGER_LABEL
            short = task_id.removeprefix("tsk_")[: fleet_service.TASK_SHORT]
            return f"{role}-{short}" if short else f"{role}-1"

    @staticmethod
    def _binary_hint(role: str) -> str:
        try:
            return harness.resolve_binary(role).binary
        except Exception:  # a hint, never a reason to fail
            return harness.DEFAULT_AGENT_BINARY

    # --- layout -----------------------------------------------------------------------

    def _header(self) -> Text:
        text = Text()
        text.append("Spawn an agent", style="bold")
        text.append(f"  {self.project.root.name or self.project.id}", style="bold cyan")
        if self.project.codename:
            text.append(f" · {self.project.codename}", style="cyan")
        return text

    def _role_prompt(self, role: str) -> str | Text:
        if role == "manager" and self._manager_live:
            return Text("manager — one per project, already running", style="dim")
        return role

    def _persona_options(self) -> list[tuple[str, str]]:
        """``(none)``, the catalogue — and any name a preset or a role default asks for
        that the catalogue lacks, so the field can show it (the spawn then refuses it
        with the reason, as the CLI would)."""
        options = [("(none)", NO_PERSONA), *((persona_choice(p), p.name) for p in self._personas)]
        known = {p.name for p in self._personas}
        wanted = {self._persona_shown, *(self._persona_default(role) for role in self._roles)}
        for name in sorted(wanted - known - {NO_PERSONA}):
            options.append((f"{name} — not one of this project's personas", name))
        return options

    def _account_options(self, statuses: Iterable[ClaudeAccountStatus]) -> list[tuple[str, str]]:
        options = [("(this shell's)", THIS_SHELL)] + [
            (account_choice(status), str(status.account.slot)) for status in statuses
        ]
        preset = self._preset_account
        if preset and preset not in {value for _, value in options}:
            options.append((f"{preset} (preset)", preset))
        return options

    def compose(self) -> ComposeResult:
        defaults = self._defaults(self._role)
        with Vertical(id="spawn-box"):
            yield Static(self._header(), id="spawn-header")
            with VerticalScroll(id="spawn-fields"):
                # Who runs it …
                with Horizontal(classes="spawn-row"):
                    yield Label("Role")
                    yield Select(
                        [(self._role_prompt(role), role) for role in self._roles],
                        value=self._role,
                        allow_blank=False,
                        id="spawn-role",
                    )
                    yield Button("Pick…", id="spawn-pick", tooltip="choose a bind or an account")
                yield Static(id="spawn-role-note", classes="spawn-note")
                with Horizontal(classes="spawn-row"):
                    yield Label("Account")
                    yield Select(
                        self._account_options([]),
                        value=self._preset_account or THIS_SHELL,
                        allow_blank=False,
                        id="spawn-account",
                    )
                yield Static(id="spawn-account-note", classes="spawn-note")
                with Horizontal(classes="spawn-row"):
                    yield Label("Binary")
                    yield Input(
                        value=self._preset_binary,
                        placeholder=self._binary_hint(self._role),
                        id="spawn-binary",
                    )
                # … then as whom.
                with Horizontal(classes="spawn-row"):
                    yield Label("Persona")
                    yield Select(
                        self._persona_options(),
                        value=self._persona_shown,
                        allow_blank=False,
                        id="spawn-persona",
                    )
                    yield Button(
                        "Import…", id="spawn-import", tooltip="import a persona, then pick it"
                    )
                yield Static(id="spawn-persona-description", classes="spawn-note")
                with Horizontal(classes="spawn-row"):
                    yield Label("Label")
                    yield Input(value=self._prefill, id="spawn-label")
                    yield Button("🎲", id="spawn-dice", tooltip="<role>-<adjective>-<animal>")
                yield Static(id="spawn-label-rule", classes="spawn-note")
                with Horizontal(classes="spawn-row"):
                    yield Label("Task")
                    yield Select(
                        [("(none)", NO_TASK), *((task_choice(t), t.id) for t in self._tasks)],
                        value=NO_TASK,
                        allow_blank=False,
                        id="spawn-task",
                    )
                yield Static(id="spawn-task-note", classes="spawn-note")
                with Horizontal(classes="spawn-row"):
                    yield Label("Worktree")
                    yield Switch(
                        value=defaults.worktree and self._git,
                        disabled=not self._git,
                        id="spawn-worktree",
                    )
                    yield Static(
                        "" if self._git else "not a git repository", id="spawn-worktree-note"
                    )
                with Horizontal(classes="spawn-row"):
                    yield Label("Permission mode")
                    yield Select(
                        permission_options(defaults.permission_mode),
                        value=defaults.permission_mode,
                        allow_blank=False,
                        id="spawn-permission",
                    )
                with Horizontal(classes="spawn-row"):
                    yield Label("Extra agent args")
                    yield Input(placeholder="e.g. --model opus", id="spawn-args")
                yield Static(id="spawn-args-error", classes="spawn-note")
                with Horizontal(classes="spawn-row"):
                    yield Label("First prompt")
                    yield TextArea(id="spawn-prompt")
            yield Static(id="spawn-status")
            with Horizontal(id="spawn-buttons"):
                yield Button("Spawn", id="spawn-submit", variant="primary")
                yield Button("Cancel", id="spawn-cancel")

    def on_mount(self) -> None:
        if self._manager_live:
            # After the Select has built its overlay's options (its own mount).
            self.call_after_refresh(self._grey_out_manager)
        self._note("#spawn-task-note", self._tasks_unavailable, style="dim")
        self._describe_persona()
        self._validate()
        if self._accounts is not None:
            self.run_worker(
                self._accounts,
                name=ACCOUNTS_WORKER,
                group=ACCOUNTS_WORKER,
                thread=True,
                exit_on_error=False,
            )
        self.query_one("#spawn-role", Select).focus()

    def _grey_out_manager(self) -> None:
        overlay = self.query_one("#spawn-role", Select).query_one(OptionList)
        overlay.disable_option_at_index(self._roles.index("manager"))

    def _note(self, selector: str, text: str | None, *, style: str = "red") -> None:
        note = self.query_one(selector, Static)
        note.update(Text(text, style=style) if text else "")
        note.display = bool(text)

    # --- validation -------------------------------------------------------------------

    def _label_problem(self, label: str) -> str | None:
        if not fleet_service.is_label(label):
            return LABEL_RULE
        if label == fleet_service.MANAGER_LABEL:
            return f"the label {fleet_service.MANAGER_LABEL!r} is reserved for the manager role"
        return None

    def _validate(self) -> bool:
        """Show every rule the form breaks right now; *Spawn* is enabled only when there is none."""
        name = self.project.root.name or self.project.id
        role_problem = (
            f"{name} already has a manager — one per project"
            if self._role == "manager" and self._manager_live
            else None
        )
        label_problem = (
            None
            if self._role == "manager"
            else self._label_problem(self.query_one("#spawn-label", Input).value)
        )
        args_problem: str | None = None
        try:
            split_agent_args(self.query_one("#spawn-args", Input).value)
        except ValueError as exc:
            args_problem = f"extra agent args: {exc}"
        self._note("#spawn-role-note", role_problem)
        self._note("#spawn-label-rule", label_problem)
        self._note("#spawn-args-error", args_problem)
        ok = role_problem is None and label_problem is None and args_problem is None
        self.query_one("#spawn-submit", Button).disabled = self._spawning or not ok
        return ok

    # --- the fields follow the role and the task until touched --------------------------

    def _task_value(self) -> str:
        value = self.query_one("#spawn-task", Select).value
        return value if isinstance(value, str) else NO_TASK

    def _persona_value(self) -> str:
        value = self.query_one("#spawn-persona", Select).value
        return value if isinstance(value, str) else NO_PERSONA

    def _relabel(self, old_role: str) -> None:
        """Re-prefill the label for the current role and task — unless the user changed it."""
        label = self.query_one("#spawn-label", Input)
        if old_role != "manager":
            self._kept_label = label.value if label.value != self._prefill else None
        self._prefill = self._label_for(self._role, self._task_value())
        if self._role == "manager" or self._kept_label is None:
            label.value = self._prefill
        else:
            label.value = self._kept_label
        locked = self._role == "manager"
        label.disabled = locked
        self.query_one("#spawn-dice", Button).disabled = locked

    @on(Select.Changed, "#spawn-role")
    def _role_changed(self, event: Select.Changed) -> None:
        role = event.value
        if not isinstance(role, str) or role == self._role:
            self._validate()
            return
        old, self._role = self._role, role
        old_defaults, new_defaults = self._defaults(old), self._defaults(role)
        self._relabel(old)
        switch = self.query_one("#spawn-worktree", Switch)
        if self._git and switch.value == old_defaults.worktree:
            switch.value = new_defaults.worktree
        mode = self.query_one("#spawn-permission", Select)
        current = mode.value
        keep = (
            current
            if isinstance(current, str) and current != old_defaults.permission_mode
            else new_defaults.permission_mode
        )
        mode.set_options(permission_options(keep))
        mode.value = keep
        if not self._persona_touched:
            # Every role default is already an option (_persona_options), so this
            # never needs set_options — which would post a Changed for "(none)".
            self._persona_shown = self._persona_default(role)
            self.query_one("#spawn-persona", Select).value = self._persona_shown
        self.query_one("#spawn-binary", Input).placeholder = self._binary_hint(role)
        self._validate()

    @on(Select.Changed, "#spawn-persona")
    def _persona_changed(self, event: Select.Changed) -> None:
        if isinstance(event.value, str) and event.value != self._persona_shown:
            self._persona_touched = True  # a value the form did not put there: the user's pick
            self._persona_shown = event.value
        self._describe_persona()

    def _describe_persona(self) -> None:
        """The description under the field — or why there is none."""
        value = self._persona_value()
        found = next((p for p in self._personas if p.name == value), None)
        if self._personas_unavailable:
            text, style = self._personas_unavailable, "dim"
        elif not value:
            text, style = "(no persona — the agent runs as its role alone)", "dim"
        elif found is None:
            text, style = (
                f"{value} is not one of this project's personas — spawn refuses it",
                "yellow",
            )
        else:
            text, style = found.description, ""
        note = self.query_one("#spawn-persona-description", Static)
        note.update(Text(text, style=style))
        note.display = True

    @on(Select.Changed, "#spawn-task")
    def _task_changed(self) -> None:
        self._relabel(self._role)
        self._validate()

    @on(Input.Changed, "#spawn-label")
    @on(Input.Changed, "#spawn-args")
    def _field_changed(self) -> None:
        self._validate()

    @on(Button.Pressed, "#spawn-dice")
    def _roll(self) -> None:
        label = dice_label(self._role)
        if label is None:
            self._note(
                "#spawn-status",
                f"🎲 no adjective-animal pair fits after {self._role!r} in {LABEL_MAX} "
                "characters — type a label",
                style="yellow",
            )
            return
        self.query_one("#spawn-label", Input).value = label

    # --- the target picker's seam --------------------------------------------------------

    @on(Button.Pressed, "#spawn-pick")
    def _request_pick(self) -> None:
        self.post_message(PickTargetRequested(self.project.id))

    def on_pick_target_requested(self, event: PickTargetRequested) -> None:
        """The dialog's own *Pick…*: the target picker in "new" order; the choice fills the form."""
        self.app.push_screen(
            AttachTargetScreen(
                self.project,
                persona=self._persona_value() or None,
                intent="new",
                accounts=self._accounts,
            ),
            callback=self._picked,
        )

    def _picked(self, target: Target | None) -> None:
        if target is None:
            return
        if target.kind == "new-account":
            self.post_message(NewAccountRequested())
            self.dismiss(None)
            return
        self.apply_target(role=target.role, binary=target.binary, account=target.account)

    def apply_target(
        self, *, role: str | None = None, binary: str | None = None, account: str | None = None
    ) -> None:
        """Fill who runs it from a picked target, as if the user had chosen each field.

        Unlike the constructor presets this acts on an open form, so it goes through
        the fields' own change handlers: a new role brings its label, worktree,
        permission mode and (untouched) persona with it, and nothing typed is lost.
        """
        if role:
            roles = self.query_one("#spawn-role", Select)
            if role not in self._roles:
                self._roles.append(role)
                roles.set_options([(self._role_prompt(r), r) for r in self._roles])
                if self._manager_live:
                    self.call_after_refresh(self._grey_out_manager)
            roles.value = role
        if binary:
            self.query_one("#spawn-binary", Input).value = binary
        if account:
            self._preset_account = account
            slots = self.query_one("#spawn-account", Select)
            slots.set_options(self._account_options(self._account_statuses))
            slots.value = account

    # --- import a persona from here ------------------------------------------------------

    @on(Button.Pressed, "#spawn-import")
    def _import_persona(self) -> None:
        self.app.push_screen(
            ImportPersonaScreen(git_common_root(self.project.root)), callback=self._imported
        )

    def _imported(self, result: personas_service.ImportResult | None) -> None:
        """Select what was just imported — re-reading the catalogue it landed in."""
        if result is None:
            return
        name = result.persona.name
        self._personas, self._personas_unavailable = self._read_personas()
        self._persona_touched = True
        self._persona_shown = name
        field = self.query_one("#spawn-persona", Select)
        field.set_options(self._persona_options())
        field.value = name
        self._describe_persona()

    # --- spawn --------------------------------------------------------------------------

    def spawn_kwargs(self) -> dict[str, Any]:
        """The keywords *Spawn* sends — ``None`` wherever the form shows the role's default."""
        defaults = self._defaults(self._role)
        label = self.query_one("#spawn-label", Input).value
        switched = self.query_one("#spawn-worktree", Switch).value
        if not self._git:
            worktree: bool | None = False if defaults.worktree else None
        else:
            worktree = None if switched == defaults.worktree else switched
        mode = self.query_one("#spawn-permission", Select).value
        account = self.query_one("#spawn-account", Select).value
        prompt = self.query_one("#spawn-prompt", TextArea).text
        chosen = self._persona_value()
        persona: str | None = chosen
        if not self._persona_touched or (
            chosen == NO_PERSONA and not self._persona_default(self._role)
        ):
            persona = None  # the role's default — or no persona where there is no default
        return {
            "label": None if self._role == "manager" or label == self._prefill else label,
            "task_id": self._task_value() or None,
            "worktree": worktree,
            "permission_mode": None if mode == defaults.permission_mode else mode,
            "binary": self.query_one("#spawn-binary", Input).value.strip() or None,
            "prompt": prompt if prompt.strip() else None,
            "agent_args": split_agent_args(self.query_one("#spawn-args", Input).value),
            "account": account if isinstance(account, str) and account != THIS_SHELL else None,
            "persona": persona,
        }

    @on(Button.Pressed, "#spawn-submit")
    def _submit(self) -> None:
        if self._spawning or not self._validate():
            return
        role, kwargs = self._role, self.spawn_kwargs()
        self._set_spawning(True)
        self.run_worker(
            lambda: fleet_service.spawn(self.project, role, **kwargs),
            name=SPAWN_WORKER,
            group=SPAWN_WORKER,
            thread=True,
            exit_on_error=False,  # a FleetError is an answer to show, not a crash
        )

    def _set_spawning(self, running: bool) -> None:
        self._spawning = running
        self.query_one("#spawn-cancel", Button).disabled = running
        self._note("#spawn-status", f"spawning {self._role} …" if running else None, style="dim")
        self._validate()

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.name == ACCOUNTS_WORKER:
            self._accounts_changed(event.worker, event.state)
        elif event.worker.name == SPAWN_WORKER:
            self._spawn_changed(event.worker, event.state)

    def _spawn_changed(self, worker: Worker[Any], state: WorkerState) -> None:
        if state is WorkerState.SUCCESS:
            receipt = worker.result
            if isinstance(receipt, fleet_service.SpawnReceipt):
                self.dismiss(receipt)
                return
            self._refused(f"the spawn answered without a receipt ({type(receipt).__name__})")
        elif state is WorkerState.ERROR:
            error = worker.error
            if isinstance(error, fleet_service.FleetError):
                self._refused(str(error))
            else:
                self._refused(f"{type(error).__name__}: {error}")
        elif state is WorkerState.CANCELLED:
            self._set_spawning(False)

    def _refused(self, reason: str) -> None:
        """The service said no (or broke): say why, stay open, let the user try again."""
        self._set_spawning(False)
        # A Text, not a markup string: the reason can carry a path with [brackets].
        self._note("#spawn-status", reason, style="bold red")

    def _accounts_changed(self, worker: Worker[Any], state: WorkerState) -> None:
        if state is WorkerState.SUCCESS and isinstance(worker.result, AccountsOverview):
            select = self.query_one("#spawn-account", Select)
            current = select.value
            self._account_statuses = list(worker.result.accounts)
            options = self._account_options(worker.result.accounts)
            select.set_options(options)
            if current in {value for _, value in options}:
                select.value = current
        elif state is WorkerState.ERROR:
            error = worker.error
            self._note(
                "#spawn-account-note",
                f"accounts unavailable — {type(error).__name__}: {error}",
                style="dim",
            )

    def action_cancel(self) -> None:
        if self._spawning:
            return  # a started spawn cannot be taken back; its answer is on the way
        self.dismiss(None)

    @on(Button.Pressed, "#spawn-cancel")
    def _cancel(self) -> None:
        self.action_cancel()
