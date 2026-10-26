"""The Spawn dialog's Model and Effort open on the owner's last pick (#16 on the board).

#338 made every fleet spawn pass the role ladder's top rung: a manager always started on
Fable at high effort, over the default the owner had saved with ``/model``. A spawn now runs
the owner's last pick, one per home whatever the role (``services.model_memory``), and the
dialog shows it: both fields open on it and keep it when the role changes, unless the new
role's settings pin a value, which they show instead; blank is the account's own default.
Untouched they are sent as ``None`` (the service decides, as for ``fleet spawn``); a changed
one is this spawn's pick, and the service remembers it (``tests/test_fleet_service.py``
holds that half).
"""

from __future__ import annotations

from typing import Any

import pytest
from textual.pilot import Pilot
from textual.widgets import Input, Select

from aisquare.cli.ui import spawn as spawn_module
from aisquare.cli.ui.spawn import ACCOUNT_DEFAULT_CHOICE, SpawnDialog
from aisquare.core.config import FleetRoleSettings, load_config, save_config
from aisquare.models import FleetAgentStatus, ProjectInfo
from aisquare.services import claude_accounts as accounts_service
from aisquare.services import fleet as fleet_service
from aisquare.services import model_memory
from tests import test_ui_spawn as spawn_suite
from tests.test_ui_spawn import (
    UNTOUCHED,
    Host,
    SpawnRecorder,
    advanced,
    drive,
    note,
    select,
    settle,
    submit,
)

# The Spawn dialog suite's fixtures, bound here so pytest finds them for this module's tests.
no_real_tmux = spawn_suite.no_real_tmux
spawns = spawn_suite.spawns
teammates = spawn_suite.teammates
git_project = spawn_suite.git_project


def fields(dialog: SpawnDialog) -> tuple[str, str]:
    """What Model and Effort show: the model's text, the effort's option."""
    effort = select(dialog, "effort")
    shown = next(prompt for prompt, value in effort._options if value == effort.value)
    return dialog.query_one("#spawn-model", Input).value, str(shown)


def _pin_reviewer() -> None:
    """``[fleet.roles.reviewer] extra_args`` naming a model and an effort: the role's own."""
    config = load_config()
    config.fleet.roles["reviewer"] = FleetRoleSettings(
        extra_args=["--model", "haiku", "--effort", "low"]
    )
    save_config(config)


def test_the_fields_open_on_the_pick_and_show_a_roles_pin_for_that_role_alone(
    git_project: ProjectInfo,
) -> None:
    model_memory.remember(model="opus", effort="xhigh")
    _pin_reviewer()

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        seen: list[Any] = [fields(dialog), dialog.query_one("#spawn-model", Input).placeholder]
        for role in ("manager", "reviewer", "tester"):
            select(dialog, "role").value = role
            await pilot.pause()
            seen.append(fields(dialog))
        return seen

    opened, placeholder, manager, reviewer, tester = drive(git_project, scenario)
    assert opened == manager == tester == ("opus", "xhigh"), "the owner's, whatever the role"
    assert reviewer == ("haiku", "low"), "the reviewer's settings pin it"
    assert placeholder.startswith(ACCOUNT_DEFAULT_CHOICE)


def test_with_nothing_picked_the_fields_are_blank_the_accounts_default(
    git_project: ProjectInfo,
) -> None:
    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> tuple[str, str]:
        return fields(dialog)

    assert drive(git_project, scenario) == ("", ACCOUNT_DEFAULT_CHOICE)


def test_untouched_fields_are_sent_as_the_default(
    git_project: ProjectInfo, spawns: SpawnRecorder
) -> None:
    model_memory.remember(model="opus", effort="xhigh")

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> None:
        await pilot.click("#spawn-submit")
        await settle(pilot)

    drive(git_project, scenario)
    assert spawns.calls == [(git_project.id, "coder", UNTOUCHED)]


def test_a_value_typed_in_stays_when_the_role_changes_and_is_sent_as_a_pick(
    git_project: ProjectInfo, spawns: SpawnRecorder
) -> None:
    """Over a role's pin too: a typed value is this spawn's own, which wins over the role's
    settings; the untouched effort shows the pin, and is sent as ``None``."""
    model_memory.remember(model="opus", effort="xhigh")
    _pin_reviewer()

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> tuple[str, str]:
        dialog.query_one("#spawn-model", Input).value = "claude-opus-5-5"
        await pilot.pause()
        select(dialog, "role").value = "reviewer"
        await pilot.pause()
        shown = fields(dialog)
        submit(dialog).press()
        await settle(pilot)
        return shown

    shown = drive(git_project, scenario)
    assert shown == ("claude-opus-5-5", "low"), "the typed model stays; the effort is the pin"
    assert spawns.calls == [(git_project.id, "reviewer", {**UNTOUCHED, "model": "claude-opus-5-5"})]


def test_a_cleared_field_is_sent_as_the_accounts_default(
    git_project: ProjectInfo, spawns: SpawnRecorder
) -> None:
    """``""``: no flag, and the service forgets the pick."""
    model_memory.remember(model="opus", effort="xhigh")

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> None:
        dialog.query_one("#spawn-model", Input).value = ""
        select(dialog, "effort").value = ""
        await pilot.pause()
        submit(dialog).press()
        await settle(pilot)

    drive(git_project, scenario)
    assert spawns.calls == [(git_project.id, "coder", {**UNTOUCHED, "model": "", "effort": ""})]


def test_the_next_dialog_opens_on_the_pick_a_spawn_remembered(
    git_project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pick the dialog sends is what ``fleet.spawn`` remembers (``model_memory.remember``,
    as the service test proves); the next dialog opens on it, for any role."""
    sent: list[dict[str, Any]] = []

    def spawn(project: ProjectInfo, role: str, **kwargs: Any) -> fleet_service.SpawnReceipt:
        sent.append(kwargs)
        model_memory.remember(model=kwargs["model"], effort=kwargs["effort"])
        return spawn_suite.receipt_for(project)

    monkeypatch.setattr(fleet_service, "spawn", spawn)

    async def pick(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> None:
        dialog.query_one("#spawn-model", Input).value = "sonnet"
        select(dialog, "effort").value = "low"
        await pilot.pause()
        submit(dialog).press()
        await settle(pilot)

    async def reopen(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> tuple[str, str]:
        select(dialog, "role").value = "validator"
        await pilot.pause()
        return fields(dialog)

    drive(git_project, pick)
    assert [(kwargs["model"], kwargs["effort"]) for kwargs in sent] == [("sonnet", "low")]
    assert drive(git_project, reopen) == ("sonnet", "low")


def test_a_model_that_is_no_model_name_disables_spawn_and_says_why(
    git_project: ProjectInfo,
) -> None:
    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        dialog.query_one("#spawn-model", Input).value = "opus --effort max"
        await pilot.pause()
        return [note(dialog, "#spawn-model-error"), submit(dialog).disabled, advanced(dialog)]

    error, disabled, section = drive(git_project, scenario)
    assert error is not None and "no model name" in error
    assert disabled is True and section.collapsed is False


def test_the_model_field_is_the_family_the_account_label_reads(
    git_project: ProjectInfo,
) -> None:
    """#277: a headroom pick reads the model's family. The label is read with the args and
    the Model field as the spawn is sent them (``None`` untouched, ``""`` cleared), and the
    service's own precedence decides (``fleet.spawn_runs``)."""
    model_memory.remember(model="opus")
    reads: list[tuple[list[str], str | None]] = []

    def counted(
        project: ProjectInfo, role: str, args: list[str], model: str | None = None
    ) -> accounts_service.LaunchDefault:
        reads.append((list(args), model))
        return accounts_service.LaunchDefault(accounts_service.AccountChoice(None, None))

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> None:
        dialog.query_one("#spawn-model", Input).value = "sonnet"
        await settle(pilot)
        dialog.query_one("#spawn-args", Input).value = "--model haiku"
        await settle(pilot)
        dialog.query_one("#spawn-args", Input).value = ""
        dialog.query_one("#spawn-model", Input).value = ""
        await settle(pilot)

    drive(git_project, scenario, defaults=counted)
    assert reads == [
        ([], None),  # untouched: the pick, as the spawn decides it
        ([], "sonnet"),
        (["--model", "haiku"], "sonnet"),
        ([], ""),  # cleared: the account's default
    ]


def test_the_account_label_reads_the_family_the_spawn_runs(
    git_project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #441 (4): with the reviewer pinned to haiku and opus typed in the form, the
    label read haiku's week while the spawn ran opus. A model the form names replaces the
    role's, as in the spawn; with none, the role's pin is the family."""
    _pin_reviewer()
    families: list[str | None] = []

    def launch_default(
        role: str | None, *, project: ProjectInfo | None = None, family: str | None = None
    ) -> accounts_service.LaunchDefault:
        families.append(family)
        return accounts_service.LaunchDefault(accounts_service.AccountChoice(None, None))

    monkeypatch.setattr(accounts_service, "launch_default", launch_default)
    spawn_module.read_launch_default(git_project, "reviewer", ["--model", "opus"])
    spawn_module.read_launch_default(git_project, "reviewer", [])
    assert families == ["opus", "haiku"]


def test_a_cleared_field_on_a_pinned_role_says_the_pin_runs(
    git_project: ProjectInfo, spawns: SpawnRecorder
) -> None:
    """Round 2 review (minor 2): over a role its settings pin, a cleared field runs the pin
    (the role's settings win over the pick), so its blank says so rather than
    ``(account default)``; elsewhere blank is still the account's default."""
    _pin_reviewer()

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[str]:
        model = dialog.query_one("#spawn-model", Input)
        seen = [model.placeholder]
        select(dialog, "role").value = "reviewer"
        await pilot.pause()
        model.value = ""
        select(dialog, "effort").value = ""
        await pilot.pause()
        seen += [model.placeholder, fields(dialog)[1]]
        select(dialog, "role").value = "tester"
        await pilot.pause()
        seen.append(model.placeholder)
        return seen

    coder, reviewer, effort, tester = drive(git_project, scenario)
    assert coder.startswith(ACCOUNT_DEFAULT_CHOICE) and tester.startswith(ACCOUNT_DEFAULT_CHOICE)
    assert "haiku" in reviewer and "reviewer" in reviewer
    assert "low" in effort and "account default" not in effort


def test_a_hand_off_locks_the_fields_to_its_teammates_own(
    git_project: ProjectInfo, teammates: list[FleetAgentStatus]
) -> None:
    """A fork or a take-over replays its teammate's launch, the model it runs now included."""
    teammates.append(spawn_suite.teammate(git_project))

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[bool]:
        model = dialog.query_one("#spawn-model", Input)
        effort = dialog.query_one("#spawn-effort", Select)
        free = [model.disabled, effort.disabled]
        select(dialog, "from").value = teammates[0].agent.id
        await pilot.pause()
        return [*free, model.disabled, effort.disabled]

    assert drive(git_project, scenario) == [False, False, True, True]
