"""The Spawn dialog — the sidebar's *Spawn agent*, over ``services.fleet.spawn``.

docs/plans/spawn-personas.md §4.1 (the field table this follows row by row) and
docs/plans/fleet-tui.md §4.1, §5.7 (the row, the label rules, the 🎲 form). The
dialog is a form in front of the one spawn the CLI runs: every field is one
keyword of :func:`aisquare.services.fleet.spawn`, and a field still showing what
it opened with is sent as ``None`` — "the role's default", exactly what an
omitted CLI flag means — so the service resolves the default at spawn time from
the config it reads then, not from the one this form read when it opened. The
prefilled label follows the same rule: sent only when the user changed it, so
the service picks the free label under the store it is writing with.

**Two tiers: three fields, then Advanced** (tsk_01m5mm2sz23b). Always on show,
in this order: Role, *Hand off from* (its *How* row only once a teammate is
picked) and Account, then the status line and the buttons. Above them, when the
caller lists the projects (``projects=``: the sidebar's *Spawn agent* does), the
**Project** field says where the agent goes; the persona-first flow and the
captain, whose project is fixed, open without it. Role's first option
is *(none) — general-purpose Claude*, ``fleet.PLAIN_ROLE``: a plain Claude Code
session on the board, with no standing cycle and no mission; ``coder`` stays
the default. Everything else is folded into one *Advanced* section (a
``Collapsible``, opened by a click or ``Enter`` on its title): *Pick…*, Binary,
Persona with *Import…*, Label with 🎲, Task, Worktree, Permission mode, Extra
agent args and First prompt. It opens by itself when a preset fills one of its
fields (``binary=``, or ``persona=`` from the persona-first flow; not the
captain's own persona) and whenever one of its fields blocks *Spawn* — a bad
label, a quoting error in the args — so *Spawn* is never disabled for a reason
folded out of sight; folded again over such a field, it opens again. Folding
changes nothing that is sent. The box sizes to what it shows: the header and the
footer are docked, and the fields between them scroll once Advanced is open on
a short terminal.

**Who runs it, then as whom** (§4.1, P4). Inside Advanced the target fields come
first — *Pick…*, Binary — and the Persona select right after them, with the
persona's description under it. It preselects the role's
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

**Changing Project** re-reads everything that hangs on the project, as the dialog does
when it opens: the header, git (the worktree switch), whether a manager already runs (the
Role prompt and its label lock), *Hand off from* and Task (reset to ``(none)``), the persona
catalogue and the role's default persona (unless one was picked) and the prefilled label
(unless it was edited). Account, Binary, Permission mode, Extra agent args and First prompt
keep what was entered, but the label of Account's no-account option (what a spawn with no
account runs on, #12) is read again for the new project, whose default account is a rung of
that ladder; and the spawn goes to the project the field shows.

**The captain** (T4): opened on the home board preset to ``captain`` — the
*Start captain*'s dialog when there is no live captain — the form starts the captain through
``services.captain.brain.start``, never ``fleet.spawn``, which refuses a captain
without its brain folder and its one server (T2). So the fields that would change
what the captain IS are fixed: the role, the label (one per home), the task (it
has none) and the extra agent args (its own: the server, no other tool). Account,
binary, permission mode, persona and a first prompt are the owner's to choose.

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

That answer closes THIS dialog and no other (review of #240, finding 5).
``Screen.dismiss`` pops whatever screen is on top (Textual 8.2), so a receipt
that landed under the import dialog closed that one and left this one on
"spawning …" for good, every way out disabled. So while a spawn runs, *Pick…*
and *Import…*, which open a screen over this one, are disabled with *Cancel*
and *Spawn*; an answer that finds another screen on top all the same waits
until this dialog is the active screen again, a refusal as much as a receipt;
and the picker's *+ New account*, which leaves the dialog, is not followed
before the answer has landed: it dismissed with ``None`` and lost the receipt.

Keys: ``Esc`` cancels; ``Tab``/``Shift+Tab`` move; the buttons are the only
submit. Nothing else is bound — the modal owns focus while it is open, and the
agent pane behind it keeps every key it had.
"""

from __future__ import annotations

import dataclasses
import random
import shlex
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from typing import Any, ClassVar

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    Collapsible,
    Input,
    Label,
    OptionList,
    RadioButton,
    RadioSet,
    Select,
    Static,
    Switch,
    TextArea,
)
from textual.worker import Worker, WorkerState

from aisquare.cli.ui.attach import AttachTargetScreen, NewAccountRequested, Target
from aisquare.cli.ui.persona_dialogs import ImportPersonaScreen
from aisquare.cli.ui.sidebar import short_path
from aisquare.cli.ui.views.app_settings import permission_options
from aisquare.core import claude_accounts as accounts_core
from aisquare.core import codenames, experimental, harness, personas
from aisquare.core.config import FleetRoleSettings, load_config
from aisquare.core.personas import Persona
from aisquare.core.store import store_session
from aisquare.models import (
    AccountsOverview,
    ClaudeAccountStatus,
    FleetAgentStatus,
    ProjectInfo,
    TeamTask,
)
from aisquare.services import claude_accounts as accounts_service
from aisquare.services import fleet as fleet_service
from aisquare.services import personas as personas_service
from aisquare.services.project import display_name

SPAWN_WORKER = "spawn-agent"
"""The worker that runs the spawn. Not ``spawn-manager`` — that is the Manager
tab's, and a worker's state change bubbles up to the app past both."""

ACCOUNTS_WORKER = "spawn-accounts"
"""The worker that reads the Claude accounts, so the dialog opens before they are read."""
DEFAULT_WORKER = "spawn-default-account"
"""The worker that reads what a spawn with no account runs on (:func:`read_launch_default`):
the registry, the config, and through the shared cache the usage a headroom pick reads."""

NO_TASK = ""
"""The Task field's ``(none)``."""

THIS_SHELL = ""
"""The Account field's first option: no ``--account`` at all, so the spawn runs where the
resolver's ladder says (``claude_accounts.choose``). Labelled with that pick
(:func:`default_account_label`), ``(this shell's)`` only when the ladder picks nothing."""
DEFAULT_PENDING = "(default)"
"""That option's label while the pick is being read, or when it could not be."""

NO_PERSONA = ""
"""The Persona field's ``(none)`` — and what ``spawn`` reads as "no persona"."""
NO_SOURCE = ""
"""The Hand off from field's ``(none)``: the plain spawn, field for field (tsk_01m3ns5a736s)."""

NO_ROLE = fleet_service.PLAIN_ROLE
"""The Role field's ``(none)``: a plain Claude Code session, no standing cycle or mission."""
NO_ROLE_CHOICE = "(none) — general-purpose Claude"
NO_ROLE_NOTE = "a plain Claude Code session on the board: no standing cycle, no mission"

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
    """``(none)``, the fleet's roles in their fixed order, then every other role ``team bind``
    knows, sorted."""
    return [
        NO_ROLE,
        *fleet_service.FLEET_ROLES,
        *sorted(set(bound) - {NO_ROLE, *fleet_service.FLEET_ROLES}),
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


def read_launch_default(
    project: ProjectInfo, role: str, agent_args: Sequence[str] = ()
) -> accounts_service.LaunchDefault:
    """What a spawn of ``role`` in ``project`` with no account runs on: the service's
    ``launch_default``, with the model family ``fleet.spawn`` passes the ladder (#277), from
    the role's configured args and then the form's ``agent_args`` (a ``--model opus`` typed
    into *Extra agent args* reads that family's week; review of #424)."""
    args = [*fleet_service.role_settings(role).extra_args, *agent_args]
    family = harness.launch_family(role, args)
    return accounts_service.launch_default(role, project=project, family=family)


def default_account_label(default: accounts_service.LaunchDefault, role: str) -> str:
    """``(default: rank 1 · work)`` — what the Account field's no-account option runs on,
    by the rung that picked it (rank, headroom, the role's binding, the project's default).

    ``(this shell's)`` only when the ladder picks nothing — no account signed in — and the
    launch leaves this shell's environment as it is; a role whose binding sets
    ``CLAUDE_CONFIG_DIR`` by hand runs on that directory instead, and says so. Before #12 on
    the board it always read ``(this shell's)``, as if the launch skipped the accounts.
    """
    account = default.choice.account
    if account is None:
        return (
            f"(default: {role}'s CLAUDE_CONFIG_DIR)"
            if default.bound_directory
            else "(this shell's)"
        )
    source = default.choice.source
    if source == "preferred":
        why = f"rank {account.position}" if account.position is not None else "preferred"
    elif source == "role binding":
        why = f"bound to {role}"
    else:
        why = source or "picked"
    return f"(default: {why} · {accounts_core.label(account)})"


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


def start_captain(kwargs: dict[str, Any]) -> fleet_service.SpawnReceipt:
    """The captain's start from the form: ``brain.start`` with the owner's choices (T4).

    ``brain.find`` first: a dead or vanished captain is ended there, so the start
    replaces it instead of being refused as a second one. The label, task, worktree
    and extra args the form holds are not the captain's to take — ``brain`` gives
    it its own. A persona left at its preset is the captain's.
    """
    from aisquare.services.captain import brain

    brain.find()
    persona = kwargs.get("persona")
    return brain.start(
        prompt=kwargs.get("prompt"),
        account=kwargs.get("account"),
        binary=kwargs.get("binary"),
        permission_mode=kwargs.get("permission_mode"),
        persona=brain.PERSONA if persona is None else persona,
    )


def source_choice(status: FleetAgentStatus) -> str:
    """One teammate in *Hand off from*: its label, state, role, task and account."""
    agent = status.agent
    slot = f"slot {agent.account_slot}" if agent.account_slot is not None else "this shell's"
    return f"{agent.label} · {status.state} · {agent.role} · {agent.task_id or 'no task'} · {slot}"


def hand_off(
    project: ProjectInfo, source: str, kwargs: dict[str, Any]
) -> fleet_service.SpawnReceipt:
    """Run a hand-off and answer as a spawn does: the started agent's receipt, every note on it.

    The dialog dismisses with a :class:`~aisquare.services.fleet.SpawnReceipt` whichever
    way it started the agent, so the shell attaches a fork or a take-over as it attaches
    any spawn.
    """
    result = fleet_service.hand_off(project, source, **kwargs)
    return dataclasses.replace(result.started, notes=list(result.notes))


@dataclasses.dataclass(frozen=True)
class PlainFields:
    """Role, Account and Persona as the plain form held them before a teammate filled them.

    What *Hand off from* gives back when it returns to ``(none)`` (review of #240, the
    stale slot).
    """

    role: str
    account: str
    preset_account: str | None
    persona: str
    persona_touched: bool


class ConfirmTakeOverScreen(ModalScreen[bool]):
    """The one question before a take-over stops its source (card tsk_01m3ns5a736s)."""

    DEFAULT_CSS = """
    ConfirmTakeOverScreen { align: center middle; }
    ConfirmTakeOverScreen #take-over-box { width: 64; max-width: 96%; height: auto;
                                           border: heavy $warning; background: $surface;
                                           padding: 1 2; }
    ConfirmTakeOverScreen #take-over-buttons { height: auto; align-horizontal: right;
                                               padding-top: 1; }
    ConfirmTakeOverScreen #take-over-buttons Button { margin-left: 2; }
    """
    BINDINGS: ClassVar = [Binding("escape", "cancel", "cancel")]

    def __init__(self, label: str) -> None:
        super().__init__()
        self.label = label

    def question(self) -> Text:
        """The question as data: the label is the user's, brackets and all."""
        text = Text()
        text.append("Stop ")
        text.append(self.label, style="bold")
        text.append(" and hand its claims to the new agent?\n")
        text.append(
            "It is stopped as a restart stops it: its claims wait for the new agent, which "
            "takes its label, task and tree.",
            style="dim",
        )
        return text

    def compose(self) -> ComposeResult:
        with Vertical(id="take-over-box"):
            yield Static(self.question(), id="take-over-question")
            with Horizontal(id="take-over-buttons"):
                yield Button("Take over", id="take-over-yes", variant="warning")
                yield Button("Cancel", id="take-over-no")

    @on(Button.Pressed, "#take-over-yes")
    def _yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#take-over-no")
    def _no(self) -> None:
        self.dismiss(False)

    def action_cancel(self) -> None:
        self.dismiss(False)


class SpawnDialog(ModalScreen[fleet_service.SpawnReceipt | None]):
    """Spawn one agent for ``project``; dismisses with the receipt, or ``None`` on cancel."""

    DEFAULT_CSS = """
    SpawnDialog { align: center middle; }
    SpawnDialog #spawn-box { width: 90; max-width: 96%; height: auto; max-height: 92%;
                             border: heavy $accent; background: $surface; padding: 0 1; }
    SpawnDialog #spawn-header { dock: top; height: auto; padding: 1 0; }
    SpawnDialog #spawn-fields { height: auto; max-height: 100%; }
    SpawnDialog #spawn-footer { dock: bottom; height: auto; }
    SpawnDialog #spawn-advanced { padding: 0; margin-top: 1; background: $surface; }
    SpawnDialog #spawn-advanced > Contents { padding: 0; }
    SpawnDialog .spawn-row { height: auto; }
    SpawnDialog .spawn-row > Label { width: 18; padding-top: 1; }
    SpawnDialog .spawn-row > Select { width: 1fr; }
    SpawnDialog .spawn-row > Input { width: 1fr; }
    SpawnDialog #spawn-dice { min-width: 7; width: 7; }
    SpawnDialog #spawn-pick { min-width: 10; width: 10; }
    SpawnDialog #spawn-import { min-width: 12; width: 12; }
    SpawnDialog .spawn-note { height: auto; padding-left: 18; color: $text-muted; }
    SpawnDialog #spawn-worktree-note { padding: 1 0 0 1; height: auto; color: $text-muted; }
    SpawnDialog #spawn-pick-note { padding: 1 0 0 1; height: auto; color: $text-muted; }
    SpawnDialog #spawn-prompt { height: 6; width: 1fr; }
    SpawnDialog #spawn-how { width: auto; height: auto; }
    SpawnDialog #spawn-fresh { margin-left: 2; }
    SpawnDialog #spawn-status { height: auto; padding: 0 0 0 0; }
    SpawnDialog #spawn-buttons { height: auto; align-horizontal: right; padding-bottom: 1; }
    SpawnDialog #spawn-buttons Button { margin-left: 2; }
    """
    BINDINGS: ClassVar = [Binding("escape", "cancel", "cancel")]

    def __init__(
        self,
        project: ProjectInfo,
        *,
        projects: Sequence[ProjectInfo] | None = None,
        persona: str | None = None,
        role: str | None = None,
        binary: str | None = None,
        account: str | None = None,
        accounts: Callable[[], AccountsOverview] | None = accounts_service.overview,
        defaults: Callable[[ProjectInfo, str, list[str]], accounts_service.LaunchDefault]
        | None = read_launch_default,
    ) -> None:
        super().__init__()
        self.project = project
        self._accounts = accounts
        self._launch_default = defaults
        self._default_model: str | None = None
        """The ``--model`` of the args the no-account option was last read with."""
        self._default_label = DEFAULT_PENDING
        """The no-account option's label: what the spawn runs on (:func:`default_account_label`)."""
        self._default_noted = False
        """Whether the Account note holds what the last default read said (a refusal), not the
        accounts read's."""
        self._fleet = fleet_service.settings()
        self._git = fleet_service.is_git_project(project.root)
        self._role = role or "coder"
        self._captain_on = experimental.captain_enabled()
        """The switch as this dialog read it, once: every captain rule here asks this, so a
        flag flipped while the dialog is open never mixes the two (review of #240, R2-m5)."""
        self._captain = self._captain_role(self._role)
        """Started through ``brain.start``, its role, label, task and args fixed (T4)."""
        self._choices = self._listed(projects)
        """The projects the Project field offers, in the order given; empty: no field, the
        project is fixed (the captain's home, the persona-first flow's)."""
        roles = role_choices(_bound_roles())
        self._roles = roles if self._role in roles else [*roles, self._role]
        self._preset_binary = binary or ""
        self._preset_account = account
        self._advanced_preset = bool(binary) or (persona is not None and not self._captain)
        """A preset fills an Advanced field, so the section opens with the form. Not the
        captain's persona: that preset is the captain's own, not a choice to show."""
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
        self._answer: tuple[Worker[Any], WorkerState] | None = None
        """A spawn's answer that found another screen on top of this one, kept until this
        dialog is the active screen again (review of #240, finding 5)."""
        self._sources, self._sources_unavailable = (
            ([], None) if self._captain else self._read_sources()
        )
        self._source: FleetAgentStatus | None = None
        """The teammate the form hands off from; ``None`` is the plain spawn."""
        self._plain: PlainFields | None = None
        """What the teammate's prefill replaced, kept while one is chosen (``_give_back``)."""

    # --- what the form reads when it opens ----------------------------------------------

    def _listed(self, projects: Sequence[ProjectInfo] | None) -> list[ProjectInfo]:
        """``projects`` for the Project field, with this dialog's own project among them."""
        if self._captain or not projects:
            return []
        listed = list(projects)
        if all(listed_project.id != self.project.id for listed_project in listed):
            listed.insert(0, self.project)
        return listed

    def _project_options(self) -> list[tuple[Text, str]]:
        """``name`` per project — and, where two share a name, its codename (else its path)."""
        names = Counter(display_name(project) for project in self._choices)
        options: list[tuple[Text, str]] = []
        for project in self._choices:
            name = display_name(project)
            label = Text(name)
            if names[name] > 1:
                label.append(f" · {project.codename or short_path(project.root)}", style="dim")
            options.append((label, project.id))
        return options

    def _read_tasks(self) -> tuple[list[TeamTask], str | None]:
        try:
            with store_session() as store:
                tasks = store.team_tasks(self.project.id)
        except Exception as exc:  # the store is busy: no task list, still a dialog
            return [], f"tasks unavailable — {type(exc).__name__}: {exc}"
        return [task for task in tasks if task.status in OPEN_TASK_STATUSES], None

    def _read_personas(self) -> tuple[list[Persona], str | None]:
        """The personas ``spawn`` accepts here: the project's catalogue, winners only."""
        try:
            found, _invalid = personas.catalogue(personas.project_root(self.project))
        except Exception as exc:  # a layer we cannot read costs the list, never the dialog
            return [], f"personas unavailable — {type(exc).__name__}: {exc}"
        return found, None

    def _read_sources(self) -> tuple[list[FleetAgentStatus], str | None]:
        """The teammates *Hand off from* offers (:func:`fleet_service.handoff_sources`)."""
        try:
            return fleet_service.handoff_sources(self.project), None
        except Exception as exc:  # tmux or the store could not be asked: no list, still a dialog
            return [], f"teammates unavailable — {type(exc).__name__}: {exc}"

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
        text.append("Start the captain" if self._captain else "Spawn an agent", style="bold")
        text.append(f"  {display_name(self.project)}", style="bold cyan")
        if self.project.codename:
            text.append(f" · {self.project.codename}", style="cyan")
        return text

    def _role_prompt(self, role: str) -> str | Text:
        if role == NO_ROLE:
            return NO_ROLE_CHOICE
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
        options = [(self._default_label, THIS_SHELL)] + [
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
                # The basic tier: where, who runs it, from whom, on which account.
                if self._choices:
                    with Horizontal(classes="spawn-row"):
                        yield Label("Project")
                        yield Select(
                            self._project_options(),
                            value=self.project.id,
                            allow_blank=False,
                            id="spawn-project",
                        )
                with Horizontal(classes="spawn-row"):
                    yield Label("Role")
                    yield Select(
                        [(self._role_prompt(role), role) for role in self._roles],
                        value=self._role,
                        allow_blank=False,
                        disabled=self._captain,
                        id="spawn-role",
                    )
                yield Static(id="spawn-role-note", classes="spawn-note")
                if not self._captain:
                    # A hand-off from a teammate (card tsk_01m3ns5a736s). ``(none)`` is
                    # the plain spawn, field for field. Each teammate is its ROW's
                    # id: two rows can carry one label (_source_changed).
                    with Horizontal(classes="spawn-row"):
                        yield Label("Hand off from")
                        yield Select(
                            [
                                ("(none)", NO_SOURCE),
                                *((source_choice(s), s.agent.id) for s in self._sources),
                            ],
                            value=NO_SOURCE,
                            allow_blank=False,
                            disabled=not self._sources,
                            id="spawn-from",
                        )
                    yield Static(id="spawn-from-note", classes="spawn-note")
                    with Horizontal(classes="spawn-row", id="spawn-how-row"):
                        yield Label("How")
                        with RadioSet(id="spawn-how"):
                            yield RadioButton("Fork", value=True, id="spawn-fork")
                            yield RadioButton("Take over", id="spawn-take-over")
                        yield Checkbox("Start fresh", id="spawn-fresh")
                    yield Static(id="spawn-how-note", classes="spawn-note")
                with Horizontal(classes="spawn-row"):
                    yield Label("Account")
                    yield Select(
                        self._account_options([]),
                        value=self._preset_account or THIS_SHELL,
                        allow_blank=False,
                        id="spawn-account",
                    )
                yield Static(id="spawn-account-note", classes="spawn-note")
                # The advanced tier, folded until asked for — or needed (_open_advanced).
                with Collapsible(
                    title="Advanced", collapsed=not self._advanced_preset, id="spawn-advanced"
                ):
                    with Horizontal(classes="spawn-row"):
                        yield Label("Bind")
                        yield Button(
                            "Pick…",
                            id="spawn-pick",
                            tooltip="choose a bind or an account",
                            disabled=self._captain,
                        )
                        yield Static(
                            "a bind or an account: fills Role, Binary and Account",
                            id="spawn-pick-note",
                        )
                    with Horizontal(classes="spawn-row"):
                        yield Label("Binary")
                        yield Input(
                            value=self._preset_binary,
                            placeholder=self._binary_hint(self._role),
                            id="spawn-binary",
                        )
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
                        yield Input(value=self._prefill, id="spawn-label", disabled=self._captain)
                        yield Button(
                            "🎲",
                            id="spawn-dice",
                            tooltip="<role>-<adjective>-<animal>",
                            disabled=self._captain,
                        )
                    yield Static(id="spawn-label-rule", classes="spawn-note")
                    with Horizontal(classes="spawn-row"):
                        yield Label("Task")
                        yield Select(
                            [("(none)", NO_TASK), *((task_choice(t), t.id) for t in self._tasks)],
                            value=NO_TASK,
                            allow_blank=False,
                            disabled=self._captain,
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
                        yield Input(
                            placeholder=(
                                "the captain's own: its server and no other tool"
                                if self._captain
                                else "e.g. --model opus"
                            ),
                            disabled=self._captain,
                            id="spawn-args",
                        )
                    yield Static(id="spawn-args-error", classes="spawn-note")
                    with Horizontal(classes="spawn-row"):
                        yield Label("First prompt")
                        yield TextArea(id="spawn-prompt")
            # Docked: the form scrolls between the header and these, which never leave sight.
            with Vertical(id="spawn-footer"):
                yield Static(id="spawn-status")
                with Horizontal(id="spawn-buttons"):
                    yield Button("Spawn", id="spawn-submit", variant="primary")
                    yield Button("Cancel", id="spawn-cancel")

    def on_mount(self) -> None:
        if self._manager_live:
            # After the Select has built its overlay's options (its own mount).
            self.call_after_refresh(self._grey_out_manager)
        self._note("#spawn-task-note", self._tasks_unavailable, style="dim")
        # Empty, a note is no row: the folded form is sized to what it shows.
        self._note("#spawn-account-note", None)
        self._note("#spawn-status", None)
        if not self._captain:
            self.query_one("#spawn-how-row").display = False
            self._note("#spawn-how-note", None)
            self._note("#spawn-from-note", self._sources_unavailable, style="dim")
        self._describe_persona()
        self._lock_other_screens()
        self._validate()
        if self._accounts is not None:
            self.run_worker(
                self._accounts,
                name=ACCOUNTS_WORKER,
                group=ACCOUNTS_WORKER,
                thread=True,
                exit_on_error=False,
            )
        self._read_default()
        # Where the agent goes comes first, when the form asks.
        self.query_one("#spawn-project" if self._choices else "#spawn-role", Select).focus()

    def _read_default(self) -> None:
        """Read what the no-account option runs on for the project, the role and the extra args
        the form holds now, off the UI thread: the registry, the config and, for a headroom
        pick, the usage cache."""
        defaults = self._launch_default
        if defaults is None:
            return
        args = self._form_args()
        self._default_model = harness.model_arg(args)
        project, role, model = self.project, self._role, self._default_model
        self.run_worker(
            lambda: (project.id, role, model, defaults(project, role, args)),
            name=DEFAULT_WORKER,
            group=DEFAULT_WORKER,
            thread=True,
            exclusive=True,
            exit_on_error=False,
        )

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
        if label == fleet_service.CAPTAIN_LABEL and self._captain_on:
            # fleet._fixed_label's on-only reservation: Spawn enabled, then the fleet refused.
            return f"the label {fleet_service.CAPTAIN_LABEL!r} is reserved for the captain"
        return None

    def _validate(self) -> bool:
        """Show every rule the form breaks right now; *Spawn* is enabled only when there is none."""
        name = display_name(self.project)
        replaces_manager = (
            self._take_over() and self._source is not None and self._source.agent.role == "manager"
        )
        role_problem = (
            f"{name} already has a manager — one per project"
            if self._role == "manager" and self._manager_live and not replaces_manager
            else None
        )
        label_problem = (
            None
            if self._label_fixed()
            else self._label_problem(self.query_one("#spawn-label", Input).value)
        )
        args_problem: str | None = None
        try:
            split_agent_args(self.query_one("#spawn-args", Input).value)
        except ValueError as exc:
            args_problem = f"extra agent args: {exc}"
        if role_problem is None and self._role == NO_ROLE:
            self._note("#spawn-role-note", NO_ROLE_NOTE, style="dim")
        else:
            self._note("#spawn-role-note", role_problem)
        self._note("#spawn-label-rule", label_problem)
        self._note("#spawn-args-error", args_problem)
        if label_problem or args_problem:
            self._open_advanced()  # never a disabled Spawn whose reason is folded away
        ok = role_problem is None and label_problem is None and args_problem is None
        self.query_one("#spawn-submit", Button).disabled = self._spawning or not ok
        return ok

    def _open_advanced(self) -> None:
        self.query_one("#spawn-advanced", Collapsible).collapsed = False

    @on(Collapsible.Collapsed, "#spawn-advanced")
    def _folded(self) -> None:
        """Folded over a field that blocks *Spawn*: ``_validate`` opens it again."""
        self._validate()

    # --- the fields follow the role and the task until touched --------------------------

    def _task_value(self) -> str:
        value = self.query_one("#spawn-task", Select).value
        return value if isinstance(value, str) else NO_TASK

    def _persona_value(self) -> str:
        value = self.query_one("#spawn-persona", Select).value
        return value if isinstance(value, str) else NO_PERSONA

    def _captain_role(self, role: str) -> bool:
        """``experimental.captain_role`` on the switch as this dialog read it."""
        return role == fleet_service.CAPTAIN_ROLE and self._captain_on

    def _label_fixed(self, role: str | None = None) -> bool:
        """Whether ``role`` (the current one by default) runs under one label only: the
        manager's, and the captain's while it is on (``fleet._fixed_label``); off,
        ``captain`` takes a label like any role."""
        role = self._role if role is None else role
        return role == "manager" or self._captain_role(role)

    def _relabel(self, old_role: str) -> None:
        """Re-prefill the label for the current role and task — unless the user changed it."""
        if self._take_over():
            return  # a take-over keeps its source's label (_apply_handoff)
        label = self.query_one("#spawn-label", Input)
        # A fixed label (the manager's, the captain's) is never the user's to keep, and a
        # locked field shows what is sent: keyed on "manager" alone, the captain's showed a
        # typed label it never sent (review of #240, R2-m5).
        if not self._label_fixed(old_role):
            self._kept_label = label.value if label.value != self._prefill else None
        self._prefill = self._label_for(self._role, self._task_value())
        if self._label_fixed() or self._kept_label is None:
            label.value = self._prefill
        else:
            label.value = self._kept_label
        locked = self._label_fixed()
        label.disabled = locked
        self.query_one("#spawn-dice", Button).disabled = locked

    @on(Select.Changed, "#spawn-role")
    def _role_changed(self) -> None:
        """The role the field holds now, not the one the message carries.

        A ``Select.Changed`` can arrive after a later change to the field: the role
        switched to tester and back to coder, and the tester one landed last. Read from
        the message, the form switched to tester under a field that showed coder, and
        spawned a tester. From the field, a late one finds its role already the form's.
        """
        role = self.query_one("#spawn-role", Select).value
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
        if self._persona_value() != self._persona_shown:
            # Not what the form put there: the owner's pick, its Changed still on the way.
            self._persona_touched = True
            self._persona_shown = self._persona_value()
        if not self._persona_touched:
            # Every role default is already an option (_persona_options, and
            # _offer_role_personas for a role that joined the list since), so this
            # never needs set_options — which would put "(none)" in the field first.
            self._put_persona(self._persona_default(role))
        self.query_one("#spawn-binary", Input).placeholder = self._binary_hint(role)
        self._describe_persona()  # the empty persona reads differently with no role
        self._read_default()  # a role's binding is a rung of the ladder
        self._validate()

    @on(Select.Changed, "#spawn-persona")
    def _persona_changed(self, event: Select.Changed) -> None:
        """The owner's pick: a value the field still holds that the form did not put there.

        The form puts its own values in silently (``_put_persona``). One that is no longer
        the field's is late, overtaken by a later change, and is nobody's pick: the role
        switched to tester and back to coder, and the ``""`` of the tester switch arrived
        after the coder one, so the coder's own persona, untouched, was sent as a choice.
        """
        value = self._persona_value()
        if event.value == value and value != self._persona_shown:
            self._persona_touched = True
            self._persona_shown = value
        self._describe_persona()

    def _put_persona(self, name: str, *, rebuild: bool = False) -> None:
        """Put ``name`` in the Persona field as the form's own value, rebuilding its options
        first if asked: no ``Select.Changed`` leaves, so none can be read as a pick."""
        self._persona_shown = name
        field = self.query_one("#spawn-persona", Select)
        with self.prevent(Select.Changed):
            if rebuild:
                field.set_options(self._persona_options())
            field.value = name
        self._describe_persona()

    def _describe_persona(self) -> None:
        """The description under the field — or why there is none."""
        value = self._persona_value()
        found = next((p for p in self._personas if p.name == value), None)
        if self._personas_unavailable:
            text, style = self._personas_unavailable, "dim"
        elif not value and self._role == NO_ROLE:
            text, style = "(plain Claude: no role, no persona)", "dim"
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
        if self._source is None:  # a hand-off decides the task itself (_apply_handoff)
            self._relabel(self._role)
        self._validate()

    # --- the Project field ----------------------------------------------------------------

    @on(Select.Changed, "#spawn-project")
    def _project_changed(self) -> None:
        """The project the field holds now, not the one the message carries (as the role's)."""
        chosen = self.query_one("#spawn-project", Select).value
        project = next((p for p in self._choices if p.id == chosen), None)
        if project is not None and project.id != self.project.id:
            self._switch_project(project)

    def _switch_project(self, project: ProjectInfo) -> None:
        """Re-read what hangs on the project, as ``__init__`` read it for the first.

        The form keeps what the project does not decide: Account, Binary, Permission mode,
        Extra agent args and First prompt, a persona or label the user chose, a worktree
        choice between two git projects. Every ``Select.Changed`` the rebuilds would send is
        held back (``prevent``) — none of them is the owner's pick — and what each one would
        have done is done here, once, at the end.
        """
        was_git = self._git
        self.project = project
        self._git = fleet_service.is_git_project(project.root)
        self.query_one("#spawn-header", Static).update(self._header())
        self._manager_live = self._read_manager_live()
        self._offer_roles()  # the manager's prompt: "already running" here, or not
        if self._manager_live:
            self.call_after_refresh(self._grey_out_manager)
        self._reset_tasks()
        # A hand-off names a teammate of the OLD project: back to the plain spawn, which also
        # gives Role, Account and Persona back (``_give_back``). After the Role field is
        # rebuilt: the role it gives back is a change the form then follows (``_role_changed``).
        self._reset_source()
        self._personas, self._personas_unavailable = self._read_personas()
        if self._persona_value() != self._persona_shown:
            self._persona_touched = True  # a pick whose Changed is still on the way
            self._persona_shown = self._persona_value()
        # A pick this project does not have is not carried over for the spawn to refuse: the
        # role's default takes its place, and the status line says so (Advanced may be folded).
        lost = self._persona_shown
        if (
            self._persona_touched
            and lost != NO_PERSONA
            and self._personas_unavailable is None
            and all(persona.name != lost for persona in self._personas)
        ):
            self._persona_touched = False
        else:
            lost = NO_PERSONA
        default = self._persona_default(self._role)
        self._put_persona(self._persona_shown if self._persona_touched else default, rebuild=True)
        self._note(
            "#spawn-status",
            f"persona {lost} is not one of {display_name(project)}'s personas — "
            f"using the role's default ({default or 'none'})"
            if lost
            else None,
            style="dim",
        )
        switch = self.query_one("#spawn-worktree", Switch)
        switch.disabled = not self._git
        if not self._git:
            switch.value = False
        elif not was_git:
            switch.value = self._defaults(self._role).worktree
        self.query_one("#spawn-worktree-note", Static).update(
            "" if self._git else "not a git repository"
        )
        self._relabel(self._role)
        self._read_default()  # the project's default account is a rung of the ladder (#12)
        self._validate()

    def _reset_tasks(self) -> None:
        """Task offers this project's open tasks, on ``(none)``."""
        self._tasks, self._tasks_unavailable = self._read_tasks()
        field = self.query_one("#spawn-task", Select)
        with self.prevent(Select.Changed):
            field.set_options([("(none)", NO_TASK), *((task_choice(t), t.id) for t in self._tasks)])
            field.value = NO_TASK
        self._note("#spawn-task-note", self._tasks_unavailable, style="dim")

    def _reset_source(self) -> None:
        """*Hand off from* offers this project's teammates, on ``(none)``: the plain spawn."""
        self._sources, self._sources_unavailable = self._read_sources()
        chosen, self._source = self._source, None
        field = self.query_one("#spawn-from", Select)
        with self.prevent(Select.Changed):
            field.set_options(
                [("(none)", NO_SOURCE), *((source_choice(s), s.agent.id) for s in self._sources)]
            )
            field.value = NO_SOURCE
        field.disabled = not self._sources
        self._note("#spawn-from-note", self._sources_unavailable, style="dim")
        if chosen is None:
            return
        self.query_one("#spawn-how-row").display = False
        with self.prevent(RadioSet.Changed, Checkbox.Changed):
            self.query_one("#spawn-fork", RadioButton).value = True
            self.query_one("#spawn-fresh", Checkbox).value = False
        self._give_back()
        self._apply_handoff()

    # --- a hand-off from a teammate (card tsk_01m3ns5a736s) -------------------------------

    @on(Select.Changed, "#spawn-from")
    def _source_changed(self, event: Select.Changed) -> None:
        """The teammate chosen, by its row's id.

        Two rows of a project can carry one label over time, an exited coder-1 and the
        coder-1 started since, and the list read when the form opened can show either or
        both. By label the form took the first row under it, and the service the newest:
        the fork or the take-over acted on a teammate the owner did not choose (review of
        #240, "also confirmed": hand-off by label). The id goes with the hand-off
        (:meth:`handoff_kwargs`), and the service acts on that row or refuses.
        """
        chosen = event.value if isinstance(event.value, str) else NO_SOURCE
        source = next((s for s in self._sources if s.agent.id == chosen), None)
        if source is self._source:
            return
        if self._source is None:
            self._plain = self._plain_fields()
        self._source = source
        self.query_one("#spawn-how-row").display = source is not None
        if source is not None:
            self._prefill_from(source)
        else:
            self._give_back()
        self._apply_handoff()

    @on(RadioSet.Changed, "#spawn-how")
    def _how_changed(self) -> None:
        self._apply_handoff()

    def _take_over(self) -> bool:
        """Whether the form takes over its teammate rather than forking it."""
        if self._source is None:
            return False
        return self.query_one("#spawn-take-over", RadioButton).value

    def _prefill_from(self, source: FleetAgentStatus) -> None:
        """Role, Account and Persona from the teammate, all still editable (the owner's call).

        The persona is marked chosen, so the role's own change handler, which runs after
        this, does not replace it with the role's default. All three from EVERY teammate:
        the Account was filled only from one that has a slot, so a teammate on this
        shell's account kept showing the slot of the one chosen before it, and that was
        sent as a choice (review of #240, the stale slot).
        """
        agent = source.agent
        if agent.role not in self._roles:
            self._roles.append(agent.role)
            self._offer_roles()
        self.query_one("#spawn-role", Select).value = agent.role
        self._persona_touched = True
        self._put_persona(agent.persona or NO_PERSONA, rebuild=True)
        own = str(agent.account_slot) if agent.account_slot is not None else THIS_SHELL
        self._preset_account = own or None
        accounts = self.query_one("#spawn-account", Select)
        accounts.set_options(self._account_options(self._account_statuses))
        accounts.value = own

    def _plain_fields(self) -> PlainFields:
        """The three fields a teammate fills, as the form holds them now."""
        account = self.query_one("#spawn-account", Select).value
        return PlainFields(
            role=self._role,
            account=account if isinstance(account, str) else THIS_SHELL,
            preset_account=self._preset_account,
            persona=self._persona_value(),
            persona_touched=self._persona_touched,
        )

    def _give_back(self) -> None:
        """``(none)`` again: Role, Account and Persona as they were before a teammate filled them.

        They stayed the teammate's, and the plain spawn went out with its role, its
        persona and its account slot, none of them chosen (review of #240, the stale
        slot). The persona goes back silently, with whether it was the owner's pick: a
        ``Select.Changed`` from here would be read as one (``_persona_changed``).
        """
        plain, self._plain = self._plain, None
        if plain is None:
            return
        self._preset_account = plain.preset_account
        options = self._account_options(self._account_statuses)
        accounts = self.query_one("#spawn-account", Select)
        accounts.set_options(options)
        if plain.account in {value for _, value in options}:
            accounts.value = plain.account
        self._persona_touched = plain.persona_touched
        self._put_persona(plain.persona, rebuild=True)
        self.query_one("#spawn-role", Select).value = plain.role

    def _apply_handoff(self) -> None:
        """Lock or free what a hand-off decides, and name the button for what it does."""
        source = self._source
        take_over = self._take_over()
        task = self.query_one("#spawn-task", Select)
        label = self.query_one("#spawn-label", Input)
        worktree = self.query_one("#spawn-worktree", Switch)
        prompt = self.query_one("#spawn-prompt", TextArea)
        for selector in ("#spawn-binary", "#spawn-permission", "#spawn-args"):
            self.query_one(selector).disabled = source is not None
        self._lock_other_screens()  # Pick…: the teammate's lock, and a running spawn's
        submit = self.query_one("#spawn-submit", Button)
        if label.disabled and not self._label_fixed():
            label.value = self._prefill  # a take-over's lock, never the user's typing
        if source is None:
            task.value = NO_TASK
            task.disabled = False
            worktree.value = self._defaults(self._role).worktree and self._git
            worktree.disabled = not self._git
            prompt.disabled = False
            self._relabel(self._role)
            submit.label = "Spawn"
            self._note("#spawn-how-note", None)
            self._validate()
            return
        agent = source.agent
        worktree.disabled = True
        if take_over:
            if agent.task_id and agent.task_id not in {t.id for t in self._tasks}:
                task.set_options(
                    [
                        ("(none)", NO_TASK),
                        *((task_choice(t), t.id) for t in self._tasks),
                        (f"{agent.task_id} ({agent.label}'s)", agent.task_id),
                    ]
                )
            task.value = agent.task_id or NO_TASK
            label.value = agent.label
            label.disabled = True
            self.query_one("#spawn-dice", Button).disabled = True
            worktree.value = agent.worktree
            prompt.disabled = True
            submit.label = "Take over"
            note = (
                f"{agent.label} is stopped, after one question, and the new agent takes over "
                "its claims, its task and its tree, resuming its session unless Start fresh "
                "is checked."
            )
        else:
            task.value = NO_TASK
            worktree.value = self._git
            prompt.disabled = False
            self._relabel(self._role)
            submit.label = "Fork"
            where = (
                f"its own worktree, cut at {agent.label}'s commit"
                if self._git
                else f"{agent.label}'s folder (not a git repository)"
            )
            note = (
                f"{agent.label} keeps running. The fork takes a new label, {where}, and no "
                "claims, and resumes its conversation unless Start fresh is checked."
            )
        task.disabled = True
        self._note("#spawn-how-note", note, style="dim")
        self._validate()

    def handoff_kwargs(self) -> dict[str, Any]:
        """The keywords a hand-off sends: ``None`` wherever the form shows the teammate's own."""
        if self._source is None:
            raise RuntimeError("no teammate chosen: a plain spawn sends spawn_kwargs")
        agent = self._source.agent
        take_over = self._take_over()
        own_slot = str(agent.account_slot) if agent.account_slot is not None else THIS_SHELL
        account = self.query_one("#spawn-account", Select).value
        persona = self._persona_value()
        label = self.query_one("#spawn-label", Input).value
        prompt = self.query_one("#spawn-prompt", TextArea).text
        if not isinstance(account, str) or account == own_slot:
            sent: str | None = None  # the teammate's own
        elif account == THIS_SHELL:
            # Explicitly this shell's: None would keep the teammate's slot (runner2-1's
            # reopen of #234).
            sent = fleet_service.THIS_SHELL_ACCOUNT
        else:
            sent = account
        return {
            "mode": "take_over" if take_over else "fork",
            "fresh": self.query_one("#spawn-fresh", Checkbox).value,
            "role": None if self._role == agent.role else self._role,
            "account": sent,
            "persona": None if persona == (agent.persona or NO_PERSONA) else persona,
            "label": None if take_over or label == self._prefill else label,
            "prompt": None if take_over or not prompt.strip() else prompt,
            # The row the form shows, not whoever carries its label by the time it runs.
            "agent_id": agent.id,
        }

    @on(Input.Changed, "#spawn-label")
    @on(Input.Changed, "#spawn-args")
    def _field_changed(self) -> None:
        self._validate()

    @on(Input.Changed, "#spawn-args")
    def _args_changed(self) -> None:
        """A ``--model`` in the extra args is the family a headroom pick reads (#277): the
        no-account option is read again when the args name another model, not on every key
        typed (review of #424)."""
        if harness.model_arg(self._form_args()) != self._default_model:
            self._read_default()

    def _form_args(self) -> list[str]:
        """*Extra agent args* as the spawn would send them; none while they do not parse
        (the form says why, and Spawn is disabled until they do)."""
        try:
            return split_agent_args(self.query_one("#spawn-args", Input).value)
        except ValueError:
            return []

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
            if self._spawning:
                # As Cancel: the answer of a started spawn is on the way, or waits for
                # this dialog to be on top (``on_screen_resume``). Leaving for the Accounts
                # page dismissed with ``None``, and the new agent's receipt and notes were
                # lost (review of #240, finding 5).
                return
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
                self._offer_roles()
                self._offer_role_personas()
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

    def _offer_roles(self) -> None:
        """Rebuild what the Role field offers once the role list has grown, its value kept.

        Silent, as ``_offer_role_personas`` is. Rebuilt, a Select falls back to its first
        option, and a ``Select.Changed`` for it reached ``_role_changed``: the form passed
        through that role on its way to the one asked for. The first is *(none)* now, with
        no default persona, and the persona it set on the way read as the owner's pick
        (``_persona_changed``), so the persona stopped following the role."""
        field = self.query_one("#spawn-role", Select)
        with self.prevent(Select.Changed):
            field.set_options([(self._role_prompt(r), r) for r in self._roles])
            field.value = self._role

    def _offer_role_personas(self) -> None:
        """Rebuild what the Persona field offers once the role list has grown, its value kept.

        ``_role_changed`` sets the field to the role's default and counts on that being
        an option, and the options were built from the roles the list held at compose. A
        role that joined later (a seat bound since, a picked agent's) with a default this
        project lacks was no option: ``InvalidSelectValueError`` in a message handler, the
        end of the TUI. Now it is offered as every missing default is (``_persona_options``).
        No ``Select.Changed`` leaves the rebuild: it passes through *(none)*, which
        ``_persona_changed`` would read as the owner's pick (review of #240, the role picker).
        """
        field = self.query_one("#spawn-persona", Select)
        current = field.value
        options = self._persona_options()
        with self.prevent(Select.Changed):
            field.set_options(options)
            if current in {value for _, value in options}:
                field.value = current

    # --- import a persona from here ------------------------------------------------------

    @on(Button.Pressed, "#spawn-import")
    def _import_persona(self) -> None:
        self.app.push_screen(
            ImportPersonaScreen(personas.project_root(self.project)), callback=self._imported
        )

    def _imported(self, result: personas_service.ImportResult | None) -> None:
        """Select what was just imported — re-reading the catalogue it landed in."""
        if result is None:
            return
        name = result.persona.name
        self._personas, self._personas_unavailable = self._read_personas()
        self._persona_touched = True
        self._put_persona(name, rebuild=True)

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
            "label": None if self._label_fixed() or label == self._prefill else label,
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
        if self._source is not None:
            source, handoff = self._source.agent.label, self.handoff_kwargs()
            if handoff["mode"] == "take_over":
                self.app.push_screen(
                    ConfirmTakeOverScreen(source),
                    callback=lambda yes: self._hand_off(source, handoff) if yes else None,
                )
                return
            self._hand_off(source, handoff)
            return
        project, role, kwargs = self.project, self._role, self.spawn_kwargs()
        self._set_spawning(True)
        self.run_worker(
            (lambda: start_captain(kwargs))
            if self._captain
            else (lambda: fleet_service.spawn(project, role, **kwargs)),
            name=SPAWN_WORKER,
            group=SPAWN_WORKER,
            thread=True,
            exit_on_error=False,  # a FleetError is an answer to show, not a crash
        )

    def _hand_off(self, source: str, kwargs: dict[str, Any]) -> None:
        project = self.project
        self._set_spawning(True)
        self.run_worker(
            lambda: hand_off(project, source, kwargs),
            name=SPAWN_WORKER,
            group=SPAWN_WORKER,
            thread=True,
            exit_on_error=False,  # a FleetError is an answer to show, not a crash
        )

    def _set_spawning(self, running: bool) -> None:
        self._spawning = running
        self.query_one("#spawn-cancel", Button).disabled = running
        if self._choices:
            self.query_one("#spawn-project", Select).disabled = running
        self._lock_other_screens()
        self._note("#spawn-status", f"spawning {self._role} …" if running else None, style="dim")
        self._validate()

    def _lock_other_screens(self) -> None:
        """*Pick…* and *Import…* open a screen over this one: neither does while a spawn runs.

        The spawn's answer has to find this dialog on top (``_spawn_changed``). Both come
        back with *Cancel* and *Spawn* once it has answered, *Pick…* as far as the form
        offers it: never to the captain, nor over a teammate's hand-off (``_apply_handoff``).
        Review of #240, finding 5.
        """
        self.query_one("#spawn-pick", Button).disabled = (
            self._spawning or self._captain or self._source is not None
        )
        # What Pick… does says nothing beside one that will not press. A running spawn
        # gives it back, so the note stays through that and the form does not jump.
        self.query_one("#spawn-pick-note").display = not self._captain and self._source is None
        self.query_one("#spawn-import", Button).disabled = self._spawning

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.name == ACCOUNTS_WORKER:
            self._accounts_changed(event.worker, event.state)
        elif event.worker.name == DEFAULT_WORKER:
            self._default_changed(event.worker, event.state)
        elif event.worker.name == SPAWN_WORKER:
            self._spawn_changed(event.worker, event.state)

    def _spawn_changed(self, worker: Worker[Any], state: WorkerState) -> None:
        if state in (WorkerState.SUCCESS, WorkerState.ERROR) and not self.is_active:
            # Another screen is on top, and ``dismiss`` pops the TOP one (Textual 8.2): a
            # receipt closed that screen and left this dialog on "spawning …" for good.
            # The answer is kept, a refusal with it, until ``on_screen_resume`` (review of
            # #240, finding 5).
            self._answer = (worker, state)
            return
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

    def on_screen_resume(self) -> None:
        """On top again: the answer that waited for this lands, a receipt or a refusal.

        Through ``call_next``, not at once. What the screen that closed handed back (a
        pick, an import) reaches this dialog as a ``call_next`` too, queued before the
        screen popped, and Textual may run it before this event or after it. Behind it
        in the same queue, the answer lands after the hand-back in either order, so
        *+ New account* always meets ``_spawning`` and is not followed (``_picked``),
        whichever answer then lands (review of #240, finding 5).
        """
        if self._answer is not None:
            self.call_next(self._land_answer)

    def _land_answer(self) -> None:
        """Give the dialog the answer it kept; kept again if a screen is back on top."""
        answer, self._answer = self._answer, None
        if answer is not None:
            self._spawn_changed(*answer)

    def _refused(self, reason: str) -> None:
        """The service said no (or broke): say why, stay open, let the user try again."""
        self._set_spawning(False)
        # A Text, not a markup string: the reason can carry a path with [brackets].
        self._note("#spawn-status", reason, style="bold red")

    def _default_changed(self, worker: Worker[Any], state: WorkerState) -> None:
        """Label the no-account option with the pick read for the project, role and model still
        in the form; a read for a project, a role or a ``--model`` the form has left since is
        dropped. A ladder that would refuse the spawn (a binding to a removed slot) says so
        under the field, until a read that does not."""
        if state is WorkerState.SUCCESS:
            project_id, role, model, default = worker.result or (None, None, None, None)
            current = (
                project_id == self.project.id
                and role == self._role
                and model == self._default_model
            )
            if current and isinstance(default, accounts_service.LaunchDefault):
                self._label_default(default_account_label(default, self._role))
                if self._default_noted:
                    self._default_noted = False
                    self._note("#spawn-account-note", None)
        elif state is WorkerState.ERROR:
            error = worker.error
            self._label_default(DEFAULT_PENDING)
            self._default_noted = True
            if isinstance(error, accounts_service.NoSuchAccount):
                self._note("#spawn-account-note", f"with no account the spawn is refused: {error}")
            else:
                self._note(
                    "#spawn-account-note",
                    f"default account unavailable — {type(error).__name__}: {error}",
                    style="dim",
                )

    def _label_default(self, label: str) -> None:
        """Relabel the no-account option, whatever the field holds kept."""
        if label == self._default_label:
            return
        self._default_label = label
        select = self.query_one("#spawn-account", Select)
        current = select.value
        options = self._account_options(self._account_statuses)
        select.set_options(options)
        if current in {value for _, value in options}:
            select.value = current
        if select.value == THIS_SHELL:
            # The same value set again redraws nothing: the closed field kept the old label.
            select.mutate_reactive(type(select).value)

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
            self._default_noted = False
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
