"""App settings, driven headless: the machine-wide form, its writes, and Reset all.

Moved here from the project's Settings tab, which now holds only the project's own
nickname (tests/test_ui_project.py). What each test asserts is the artefact: the
bytes ``config.toml`` holds after Save, the backup Reset all keeps.
"""

from __future__ import annotations

import asyncio
import tomllib
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any, TypeVar

import pytest
from textual.app import App, ComposeResult
from textual.notifications import SeverityLevel
from textual.pilot import Pilot
from textual.widgets import Button, Input, Select, Static, Switch

from aisquare.cli.ui.views.app_settings import AppSettingsView, ConfirmReset
from aisquare.core import paths
from aisquare.core.config import AppConfig, load_config, overrides, save_config
from aisquare.services import fleet as fleet_service
from aisquare.services import settings as settings_service
from tests.ui_workers import settle_page

T = TypeVar("T")


class AppHost(App[None]):
    """A bare app around one ``AppSettingsView`` that records every notification."""

    def __init__(self) -> None:
        super().__init__()
        self.notices: list[tuple[str, str]] = []

    def compose(self) -> ComposeResult:
        yield AppSettingsView(id="app-settings")

    def notify(
        self,
        message: str,
        *,
        title: str = "",
        severity: SeverityLevel = "information",
        timeout: float | None = None,
        markup: bool = True,
    ) -> None:
        self.notices.append((message, severity))
        super().notify(message, title=title, severity=severity, timeout=timeout, markup=markup)


def drive(scenario: Callable[[Pilot[None], AppHost], Coroutine[Any, Any, T]]) -> T:
    """Run ``scenario`` against a mounted App settings view and return what it observed."""

    async def run() -> T:
        host = AppHost()
        async with host.run_test(size=(120, 50)) as pilot:
            await pilot.pause()
            return await scenario(pilot, host)

    return asyncio.run(run())


async def settle(pilot: Pilot[None]) -> None:
    """Let the page go quiet: every worker of ours done, nothing queued."""
    await settle_page(pilot.app)


def _config_toml() -> dict[str, Any]:
    return tomllib.loads(paths.config_path().read_text(encoding="utf-8"))


def test_the_view_is_titled_app_settings_and_holds_no_project_field() -> None:
    async def scenario(pilot: Pilot[None], host: AppHost) -> tuple[str, list[str]]:
        title = str(host.query_one("#settings-title", Static).render())
        missing = [wid for wid in ("#codename", "#nickname") if host.query(wid)]
        return title, missing

    title, project_fields = drive(scenario)
    assert title == "App settings"
    assert project_fields == []


def test_settings_saves_a_changed_permission_mode_to_config_toml() -> None:
    assert not paths.config_path().exists()  # nothing written before the user saves

    async def scenario(pilot: Pilot[None], host: AppHost) -> list[tuple[str, str]]:
        await pilot.pause()
        host.query_one("#role-perm-coder", Select).value = "plan"
        host.query_one("#worktree-dir", Input).value = ".fleet-trees"
        host.query_one("#save-settings", Button).press()
        await settle(pilot)
        return host.notices

    notices = drive(scenario)
    assert any(m.startswith("✓ settings saved") for m, _ in notices), notices
    fleet_on_disk = _config_toml()["fleet"]
    on_disk = fleet_on_disk["roles"]
    assert on_disk["coder"]["permission_mode"] == "plan"  # the bytes, not the widget
    assert on_disk["coder"]["worktree"] is True  # the role's table whole, for older builds
    assert load_config().fleet.roles["coder"].worktree is True  # untouched fields survive
    assert on_disk["manager"] == {"permission_mode": "auto", "worktree": False, "extra_args": []}
    assert fleet_on_disk["worktree_dir"] == ".fleet-trees"  # the worktree root (§4.2)
    assert "escape_key" not in fleet_on_disk  # an untouched fleet field keeps its default
    assert load_config().fleet.escape_key == "f12"
    assert load_config().fleet.roles["coder"].permission_mode == "plan"


def test_settings_rejects_a_bad_agent_cap_and_writes_nothing() -> None:
    async def scenario(pilot: Pilot[None], host: AppHost) -> list[tuple[str, str]]:
        await pilot.pause()
        host.query_one("#max-agents", Input).value = "0"
        host.query_one("#save-settings", Button).press()
        await settle(pilot)
        return host.notices

    notices = drive(scenario)
    assert any("at least 1" in m and severity == "error" for m, severity in notices), notices
    assert not paths.config_path().exists()  # a refused form never reaches the writer


def test_settings_binds_an_account_per_role_and_a_cleared_one_leaves_no_empty_profile() -> None:
    """The select beside each role is `team bind <role> --account` with a mouse (#145)."""
    from aisquare.core import claude_accounts as accounts_core

    accounts_core.create_account()  # slot 2, under the isolated AISQUARE_HOME

    async def scenario(
        pilot: Pilot[None], host: AppHost
    ) -> tuple[list[str], list[tuple[str, str]]]:
        await settle(pilot)  # the slots arrive from a worker
        select = host.query_one("#role-acct-coder", Select)
        labels = [str(label) for label, _value in select._options]
        select.value = "2"
        host.query_one("#save-settings", Button).press()
        await settle(pilot)
        return labels, host.notices

    labels, notices = drive(scenario)
    assert any(m.startswith("✓ settings saved") for m, _ in notices), notices
    assert labels[0] == "(no account binding)"
    assert any(label.startswith("2 · account 2") for label in labels), labels
    assert load_config().team.profiles["coder"].account == "2"  # the binding's one home
    assert _config_toml()["team"]["profiles"]["coder"]["account"] == "2"  # the bytes
    assert "manager" not in load_config().team.profiles  # an untouched role gains no profile

    async def clear(pilot: Pilot[None], host: AppHost) -> str | None:
        await settle(pilot)
        select = host.query_one("#role-acct-coder", Select)
        shown_value = select.value
        select.value = ""
        host.query_one("#save-settings", Button).press()
        await settle(pilot)
        return str(shown_value)

    shown_value = drive(clear)
    assert shown_value == "2"  # the form opened on what the file held
    assert "coder" not in load_config().team.profiles  # nothing else bound: the table goes


def test_settings_clearing_an_account_keeps_a_binding_that_still_reads_a_file(
    tmp_path: Path,
) -> None:
    """Follow-up of #257: a binding is empty once its account goes only when nothing else
    is bound, and `env_files` (`team bind --env-file`, a reviewer's GH_TOKEN) is something.
    Read as `bin or env or args`, clearing the account dropped the token file with it."""
    from aisquare.core import claude_accounts as accounts_core
    from aisquare.services import settings as settings_service

    accounts_core.create_account()  # slot 2, under the isolated AISQUARE_HOME
    token = tmp_path / "coder-token"
    token.write_text("secret\n", encoding="utf-8")
    settings_service.bind_role("coder", env_files={"GH_TOKEN": str(token)}, account="2")

    async def clear(pilot: Pilot[None], host: AppHost) -> None:
        await settle(pilot)
        host.query_one("#role-acct-coder", Select).value = ""
        host.query_one("#save-settings", Button).press()
        await settle(pilot)

    drive(clear)
    profile = load_config().team.profiles["coder"]
    assert profile.account is None and profile.env_files == {"GH_TOKEN": str(token)}


def test_settings_save_leaves_the_bindings_it_did_not_change_as_the_file_has_them() -> None:
    """Final review of #203, accounts F3: Save wrote every role's account select back, so a
    binding changed since the tab was read was reverted by a save of any other field. The
    sharp case is ``accounts remove``, run from the Accounts page of the same app: it clears
    a binding to a slot that had no login, so the next ``add`` into that slot is not
    inherited by the role, and the kept-alive tab put the number straight back."""
    from aisquare.core import claude_accounts as accounts_core
    from aisquare.services import claude_accounts as accounts_service
    from aisquare.services import settings as settings_service

    removed = accounts_core.create_account()  # slot 2, never signed in
    accounts_core.create_account()  # slot 3
    settings_service.bind_role("coder", account="2")
    settings_service.bind_role("tester", account="2")

    async def scenario(
        pilot: Pilot[None], host: AppHost
    ) -> tuple[list[str], list[tuple[str, str]]]:
        await settle(pilot)  # the form holds coder → 2 and tester → 2
        notes: list[str] = []
        accounts_service.remove(removed, notes=notes)  # the Accounts page's Remove
        settings_service.bind_role("reviewer", account="3")  # `team bind` in another shell
        host.query_one("#role-acct-tester", Select).value = "3"  # the one change made here
        host.query_one("#max-agents", Input).value = "7"
        host.query_one("#save-settings", Button).press()
        await settle(pilot)
        return notes, host.notices

    notes, notices = drive(scenario)
    assert any(m.startswith("✓ settings saved") for m, _ in notices), notices
    assert any("role coder was bound to slot 2" in note for note in notes), notes
    profiles = load_config().team.profiles
    assert "coder" not in profiles or profiles["coder"].account is None  # the clear stands
    assert profiles["reviewer"].account == "3"  # the other shell's binding stands
    assert profiles["tester"].account == "3"  # what the operator changed here lands
    assert load_config().fleet.max_agents_per_project == 7
    assert accounts_core.create_account().slot == 2  # the slot the stale "2" would have named


def test_the_settings_form_reads_its_file_once_and_the_slots_off_the_ui_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review of #205, fourth round: the constructor read ``config.toml`` three times —
    ``[fleet]``, ``[accounts]``, the bindings — and listed the accounts, a store open, a
    directory scan and maybe a reconcile write, on the event loop. One read of the file;
    the slots come from a thread worker, and the select keeps what it showed meanwhile."""
    import threading

    from aisquare.cli.ui.views import app_settings as settings_view
    from aisquare.core import claude_accounts as accounts_core
    from aisquare.services import claude_accounts as accounts_service
    from aisquare.services import settings as settings_service

    accounts_core.create_account()  # slot 2
    settings_service.bind_role("coder", account="2")
    loads: list[str] = []
    for module in (settings_view, fleet_service, accounts_service, settings_service):
        real_load = module.load_config

        def counted(real: Any = real_load, name: str = module.__name__) -> Any:
            loads.append(name)
            return real()

        monkeypatch.setattr(module, "load_config", counted)
    listed: list[str] = []
    real_list = accounts_service.list_accounts

    def list_accounts() -> Any:
        listed.append(threading.current_thread().name)
        return real_list()

    monkeypatch.setattr(accounts_service, "list_accounts", list_accounts)

    form = AppSettingsView()
    assert len(loads) == 1 and listed == []  # the constructor reads the file once, lists nothing

    async def scenario(pilot: Pilot[None], host: AppHost) -> tuple[list[str], str]:
        await settle(pilot)
        select = host.query_one("#role-acct-coder", Select)
        return [str(label) for label, _value in select._options], str(select.value)

    labels, value = drive(scenario)
    assert listed and threading.main_thread().name not in listed
    assert any(label.startswith("2 · account 2") for label in labels), labels
    assert value == "2"  # the binding, shown before the slots arrived and after
    assert form.accounts.pick == "headroom" and form.fleet.escape_key == "f12"


def test_a_slot_removed_between_two_reads_falls_back_to_the_binding_on_the_form() -> None:
    """The slots arrive from a worker, so a select can hold a slot an earlier answer offered
    and the next one does not: set as its value, the select refuses it and the handler
    raises. It falls back to what the file binds instead."""
    import shutil

    from aisquare.core import claude_accounts as accounts_core

    accounts_core.create_account()  # slot 2
    third = accounts_core.create_account()  # slot 3, removed below

    async def scenario(pilot: Pilot[None], host: AppHost) -> tuple[str, list[str]]:
        await settle(pilot)
        select = host.query_one("#role-acct-coder", Select)
        select.value = "3"
        shutil.rmtree(third.config_dir)
        host.query_one(AppSettingsView)._load_accounts()
        await settle(pilot)
        return str(select.value), [str(value) for _label, value in select._options]

    value, offered = drive(scenario)
    assert value == ""  # nothing bound: "no account binding"
    assert "3" not in offered and "2" in offered


def test_settings_saves_the_accounts_section_and_rejects_a_bad_line() -> None:
    """`[accounts]` (#146) on the same form, through the same one writer."""

    async def scenario(pilot: Pilot[None], host: AppHost) -> list[tuple[str, str]]:
        await pilot.pause()
        host.query_one("#accounts-pick", Select).value = "default"
        host.query_one("#accounts-switch-at", Input).value = "70"
        host.query_one("#accounts-on-limit", Select).value = "wait"
        host.query_one("#accounts-wait-minutes", Input).value = "5"
        host.query_one("#save-settings", Button).press()
        await settle(pilot)
        return host.notices

    notices = drive(scenario)
    assert any(m.startswith("✓ settings saved") for m, _ in notices), notices
    on_disk = _config_toml()["accounts"]
    assert on_disk == {
        "pick": "default",
        "switch_at": 70,
        "on_limit": "wait",
        "wait_if_reset_within_minutes": 5,
    }
    assert load_config().accounts.pick == "default"

    async def bad(pilot: Pilot[None], host: AppHost) -> tuple[str, list[tuple[str, str]]]:
        await pilot.pause()
        shown_pick = str(host.query_one("#accounts-pick", Select).value)
        host.query_one("#accounts-switch-at", Input).value = "250"
        host.query_one("#save-settings", Button).press()
        await settle(pilot)
        return shown_pick, host.notices

    shown_pick, notices = drive(bad)
    assert shown_pick == "default"  # the form opened on what the file holds
    assert any("between 1 and 100" in m and sev == "error" for m, sev in notices), notices
    assert load_config().accounts.switch_at == 70  # a refused form never reaches the writer


def test_settings_switches_what_the_accounts_share() -> None:
    """``share_trust`` (on by default) and ``share_settings`` (off) are two switches in the
    accounts section; Save writes the one changed and nothing else."""

    async def scenario(pilot: Pilot[None], host: AppHost) -> tuple[bool, bool]:
        await pilot.pause()
        shown = (
            host.query_one("#accounts-share-trust", Switch).value,
            host.query_one("#accounts-share-settings", Switch).value,
        )
        host.query_one("#accounts-share-trust", Switch).value = False
        host.query_one("#save-settings", Button).press()
        await settle(pilot)
        return shown

    assert drive(scenario) == (True, False)  # the defaults
    assert _config_toml()["accounts"] == {"share_trust": False}
    assert load_config().accounts.share_trust is False

    async def reopened(pilot: Pilot[None], host: AppHost) -> bool:
        await pilot.pause()
        host.query_one("#accounts-share-settings", Switch).value = True
        host.query_one("#save-settings", Button).press()
        await settle(pilot)
        return host.query_one("#accounts-share-trust", Switch).value

    assert drive(reopened) is False  # the form opened on what the file holds
    assert _config_toml()["accounts"] == {"share_trust": False, "share_settings": True}


def test_reset_all_asks_first_then_keeps_a_backup_and_shows_the_defaults() -> None:
    """Reset all is a dialog first: Cancel changes nothing; Reset all moves the file aside to a
    backup it names, and the form shows the defaults."""
    config = load_config()
    config.fleet.max_agents_per_project = 9
    config.accounts.pick = "default"
    save_config(config)
    before = paths.config_path().read_bytes()

    async def scenario(pilot: Pilot[None], host: AppHost) -> dict[str, object]:
        seen: dict[str, object] = {}
        host.query_one("#reset-settings", Button).press()
        await settle(pilot)
        seen["asked"] = isinstance(host.screen, ConfirmReset)
        seen["explained"] = str(host.screen.query_one("#reset-explained", Static).render())
        host.screen.query_one("#reset-cancel", Button).press()
        await settle(pilot)
        seen["after_cancel"] = paths.config_path().read_bytes()
        host.query_one("#reset-settings", Button).press()
        await settle(pilot)
        host.screen.query_one("#reset-confirm", Button).press()
        await settle(pilot)
        seen["form"] = (
            host.query_one("#max-agents", Input).value,
            str(host.query_one("#accounts-pick", Select).value),
        )
        seen["notices"] = host.notices
        return seen

    seen = drive(scenario)
    assert seen["asked"] is True
    assert "config.toml.bak-" in str(seen["explained"]), "the dialog names the backup it keeps"
    assert seen["after_cancel"] == before, "Cancel changes nothing"
    assert not paths.config_path().exists(), "every setting is its default now"
    backups = sorted(paths.config_path().parent.glob("config.toml.bak-*"))
    assert len(backups) == 1 and backups[0].read_bytes() == before, "the file kept, unchanged"
    defaults = load_config()
    assert seen["form"] == (str(defaults.fleet.max_agents_per_project), defaults.accounts.pick)
    notices = seen["notices"]
    assert isinstance(notices, list)
    assert any(str(backups[0]) in m for m, _ in notices), notices


def test_reset_all_with_nothing_changed_says_so() -> None:
    assert not paths.config_path().exists()

    async def scenario(pilot: Pilot[None], host: AppHost) -> list[tuple[str, str]]:
        host.query_one("#reset-settings", Button).press()
        await settle(pilot)
        host.screen.query_one("#reset-confirm", Button).press()
        await settle(pilot)
        return host.notices

    notices = drive(scenario)
    assert any("at its default already" in m for m, _ in notices), notices
    assert not list(paths.config_path().parent.glob("config.toml.bak-*"))


def test_reset_all_of_a_file_holding_no_setting_keeps_no_backup() -> None:
    """A save of the defaults leaves a file (its version, the role tables): Reset all of it
    backed it up and said "yours are kept" (review of #261, m8)."""
    save_config(load_config())
    assert paths.config_path().exists()

    async def scenario(pilot: Pilot[None], host: AppHost) -> list[tuple[str, str]]:
        host.query_one("#reset-settings", Button).press()
        await settle(pilot)
        host.screen.query_one("#reset-confirm", Button).press()
        await settle(pilot)
        return host.notices

    notices = drive(scenario)
    assert any(m.startswith("nothing to reset") for m, _ in notices), notices
    assert not list(paths.config_path().parent.glob("config.toml.bak-*"))


def test_a_form_left_open_saves_only_what_was_changed_on_it() -> None:
    """The reviewer's repro (review of #261, m7): the file holds ``escape_key = "f11"`` and
    ``switch_at = 70`` when App settings opens. From a shell, ``config set accounts.pick
    default``; Save with max agents changed keeps it. Then ``config reset``; Save with max
    agents changed again keeps the reset: the form wrote back every field it showed, and
    undid both."""
    config = load_config()
    config.fleet.escape_key = "f11"
    config.accounts.switch_at = 70
    save_config(config)

    async def scenario(pilot: Pilot[None], host: AppHost) -> dict[str, object]:
        seen: dict[str, object] = {}
        settings_service.set_value("accounts.pick", "default")  # a shell, while it is open
        host.query_one("#max-agents", Input).value = "5"
        host.query_one("#save-settings", Button).press()
        await settle(pilot)
        seen["set"] = load_config()
        settings_service.reset_settings()  # a shell again: the form still shows f11, 70
        host.query_one("#max-agents", Input).value = "6"
        host.query_one("#save-settings", Button).press()
        await settle(pilot)
        seen["reset"] = load_config()
        seen["form"] = host.query_one("#escape-key", Input).value
        return seen

    seen = drive(scenario)
    kept = seen["set"]
    assert isinstance(kept, AppConfig)
    assert kept.accounts.pick == "default", "the shell's config set stands"
    assert (kept.fleet.escape_key, kept.accounts.switch_at) == ("f11", 70)
    assert kept.fleet.max_agents_per_project == 5
    after = seen["reset"]
    assert isinstance(after, AppConfig)
    assert overrides(after) == {"fleet": {"max_agents_per_project": 6}}, "the reset stands"
    assert seen["form"] == "f12", "after Save the form shows what the file holds"


def test_a_role_added_by_hand_while_the_form_is_open_costs_nothing_on_save() -> None:
    """Review of #259, round 21: ``[fleet.roles.designer]`` written by hand, then Reload,
    put ``designer`` in the form's roles with no row on it, and the next Save looked up
    ``#perm-designer`` unguarded: ``NoMatches`` out of the button handler, and the fleet
    UI exited. The row is shown after a reopen; until then Save leaves the role as the
    file has it."""
    from aisquare.core.config import FleetRoleSettings

    async def scenario(pilot: Pilot[None], host: AppHost) -> list[tuple[str, str]]:
        config = load_config()  # a hand edit, while the form is open
        config.fleet.roles["designer"] = FleetRoleSettings(permission_mode="plan")
        save_config(config)
        host.query_one("#reload-settings", Button).press()
        await settle(pilot)
        host.query_one("#role-perm-coder", Select).value = "acceptEdits"
        host.query_one("#save-settings", Button).press()
        await settle(pilot)
        host.query_one("#max-agents", Input).value = "4"
        host.query_one("#save-settings", Button).press()  # and again, from the reloaded form
        await settle(pilot)
        assert not host.query("#role-perm-designer")  # no row yet: a reopen shows it
        return host.notices

    notices = drive(scenario)
    assert [m for m, severity in notices if severity == "error"] == []
    assert sum(m.startswith("✓ settings saved") for m, _ in notices) == 2, notices
    roles = _config_toml()["fleet"]["roles"]
    assert roles["coder"]["permission_mode"] == "acceptEdits"
    assert roles["designer"]["permission_mode"] == "plan"  # the hand edit stands
    assert load_config().fleet.max_agents_per_project == 4

    async def reopened(pilot: Pilot[None], host: AppHost) -> object:
        return host.query_one("#role-perm-designer", Select).value

    assert drive(reopened) == "plan"


def test_two_roles_whose_names_differ_only_in_punctuation_keep_their_own_rows() -> None:
    """Review of #259, round 22, 3/9: a widget id turned every character outside
    ``[A-Za-z0-9_-]`` into ``-``, so ``qa.ui`` and ``qa-ui`` shared one id. Each role's
    widgets sit in their own row, so Textual raised nothing; every lookup found the first
    row's. The second row was shown and never read, and Save wrote the first row's values
    to both roles. Each role has its own id now, and Save changes the row it was asked to."""
    from aisquare.cli.ui.views.app_settings import widget_suffix
    from aisquare.core.config import FleetRoleSettings

    names = ["qa.ui", "qa-ui", "qa_ui", "qa_2e_ui", "测试", "审查", "coder", "ui-tester"]
    assert len({widget_suffix(name) for name in names}) == len(names)
    assert widget_suffix("coder") == "coder" and widget_suffix("ui-tester") == "ui-tester"

    config = load_config()
    config.fleet.roles["qa.ui"] = FleetRoleSettings(permission_mode="plan", worktree=True)
    config.fleet.roles["qa-ui"] = FleetRoleSettings(permission_mode="default", worktree=True)
    save_config(config)

    async def scenario(pilot: Pilot[None], host: AppHost) -> list[object]:
        dotted, dashed = (f"#role-perm-{widget_suffix(role)}" for role in ("qa.ui", "qa-ui"))
        shown = [host.query_one(dotted, Select).value, host.query_one(dashed, Select).value]
        host.query_one(f"#role-worktree-{widget_suffix('qa-ui')}", Switch).value = False
        host.query_one("#save-settings", Button).press()
        await settle(pilot)
        return shown

    assert drive(scenario) == ["plan", "default"], "each row shows its own role"
    roles = load_config().fleet.roles
    assert (roles["qa.ui"].permission_mode, roles["qa.ui"].worktree) == ("plan", True)
    assert (roles["qa-ui"].permission_mode, roles["qa-ui"].worktree) == ("default", False)


def test_a_role_named_like_a_fixed_field_neither_takes_its_id_nor_takes_the_form_down() -> None:
    """Review of #259, round 23, 2/10: ``[fleet.roles.dir]`` gave its worktree switch the id
    ``worktree-dir``, the worktree root's ``Input``. The two sit in different rows, so
    Textual raised no ``DuplicateIds``, and ``query_one("#worktree-dir", Input)`` found the
    role's ``Switch`` first: ``WrongType`` out of ``on_mount``, and the fleet UI went down
    at App settings. Role names are free TOML keys, so every per-role id sits under a
    prefix no fixed id has, and each field still saves what it shows."""
    from aisquare.cli.ui.views.app_settings import role_order
    from aisquare.core.config import FleetRoleSettings

    config = load_config()
    for role in ("dir", "qa.ui", "qa-ui", "max-agents"):
        config.fleet.roles[role] = FleetRoleSettings(permission_mode="plan", worktree=True)
    save_config(config)
    roles = role_order(load_config().fleet)

    async def scenario(pilot: Pilot[None], host: AppHost) -> list[str]:
        form = host.query_one(AppSettingsView)
        fields = form.query("Input, Select, Switch, Button")
        ids = [str(widget.id) for widget in fields if widget.id]
        host.query_one("#worktree-dir", Input).value = ".trees"
        from aisquare.cli.ui.views.app_settings import role_widget_id  # the fix's own name

        host.query_one(f"#{role_widget_id('worktree', 'dir')}", Switch).value = False
        host.query_one(f"#{role_widget_id('perm', 'qa-ui')}", Select).value = "acceptEdits"
        host.query_one("#save-settings", Button).press()
        await settle(pilot)
        return ids

    ids = drive(scenario)
    from aisquare.cli.ui.views.app_settings import ROLE_ID_PREFIX, role_widget_id

    assert len(ids) == len(set(ids)), "one field per id, on the whole form"
    assert {wid for wid in ids if wid.startswith(ROLE_ID_PREFIX)} == {
        role_widget_id(kind, role)
        for kind in ("perm", "worktree", "persona", "acct")
        for role in roles
    }, "the prefix is the roles' alone"
    saved = load_config().fleet
    assert saved.worktree_dir == ".trees"
    assert (saved.roles["dir"].permission_mode, saved.roles["dir"].worktree) == ("plan", False)
    assert saved.roles["qa-ui"].permission_mode == "acceptEdits"
    assert saved.roles["qa.ui"].permission_mode == "plan"


def test_role_names_modes_and_bindings_written_by_hand_are_shown_as_text_not_markup() -> None:
    """Review of #259, round 23, 4/10: ``Label(role)`` read the role's name as Textual
    markup, and role names are free TOML keys. ``[fleet.roles."qa[x]"]`` showed as ``qa``,
    and ``[fleet.roles."a[/b]c"]`` raised ``MarkupError`` when its row rendered, which took
    the fleet UI down at App settings. The same row's selects read a mode and an account
    binding written by hand as markup too. Each is shown as it is written."""
    from textual.widgets import Label

    from aisquare.core.config import FleetRoleSettings, RoleLaunchProfile

    config = load_config()
    config.fleet.roles["a[/b]c"] = FleetRoleSettings(permission_mode="x[/y]")
    config.fleet.roles["qa[x]"] = FleetRoleSettings(permission_mode="plan")
    config.team.profiles["a[/b]c"] = RoleLaunchProfile(account="w[/z]")
    save_config(config)

    async def scenario(pilot: Pilot[None], host: AppHost) -> tuple[list[str], list[str]]:
        await settle(pilot)
        form = host.query_one(AppSettingsView)
        names = [str(label.render()) for label in form.query(Label)]
        prompts = [
            str(prompt) for select in form.query(Select) for prompt, _value in select._options
        ]
        host.query_one("#max-agents", Input).value = "5"
        host.query_one("#save-settings", Button).press()
        await settle(pilot)
        return names, prompts

    names, prompts = drive(scenario)

    assert "a[/b]c" in names and "qa[x]" in names, names
    assert "x[/y] (custom)" in prompts and "w[/z] (as bound)" in prompts, prompts
    saved = load_config()
    assert saved.fleet.max_agents_per_project == 5
    assert saved.fleet.roles["a[/b]c"].permission_mode == "x[/y]", "the hand edit stands"
    assert saved.team.profiles["a[/b]c"].account == "w[/z]"


# --- the persona per role (#201) ------------------------------------------------------------


def _persona_prompts(host: AppHost, role: str) -> list[str]:
    from textual.widgets import OptionList

    from aisquare.cli.ui.views.app_settings import role_widget_id

    field = host.query_one(f"#{role_widget_id('persona', role)}", Select)
    overlay = field.query_one(OptionList)
    return [str(overlay.get_option_at_index(i).prompt) for i in range(overlay.option_count)]


def test_a_roles_persona_is_saved_to_config_toml_and_read_back() -> None:
    async def scenario(pilot: Pilot[None], host: AppHost) -> list[tuple[str, str]]:
        await pilot.pause()
        host.query_one("#role-persona-coder", Select).value = "skeptic"
        host.query_one("#save-settings", Button).press()
        await settle(pilot)
        return host.notices

    notices = drive(scenario)
    assert any(m.startswith("✓ settings saved") for m, _ in notices), notices
    roles = _config_toml()["fleet"]["roles"]
    assert roles["coder"]["persona"] == "skeptic"  # the bytes, under the coder's own table
    assert "persona" not in roles.get("tester", {})
    assert load_config().fleet.roles["coder"].persona == "skeptic"


def test_choosing_none_removes_the_roles_persona_rather_than_writing_an_empty_one() -> None:
    from aisquare.core.config import FleetRoleSettings

    config = load_config()
    config.fleet.roles["coder"] = FleetRoleSettings(persona="skeptic")
    save_config(config)

    async def scenario(pilot: Pilot[None], host: AppHost) -> object:
        await pilot.pause()
        field = host.query_one("#role-persona-coder", Select)
        before = field.value
        field.value = ""
        host.query_one("#save-settings", Button).press()
        await settle(pilot)
        return before

    assert drive(scenario) == "skeptic"
    assert "persona" not in _config_toml()["fleet"]["roles"]["coder"]
    assert load_config().fleet.roles["coder"].persona is None


def test_a_project_only_persona_is_never_offered_as_a_machine_wide_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #240, "also confirmed": the form saved a persona only the open project
    holds as ``[fleet.roles.<role>].persona``, a machine-wide default, so every other
    project's spawn of that role refused it. The form lists the personas every project
    can resolve — the user's and the bundled ones — and a configured name it lacks shows
    as ``(custom)``, kept rather than dropped by a save."""
    from aisquare.core.config import FleetRoleSettings

    repo = tmp_path / "repo"
    skill = repo / ".aisquare" / "personas" / "local-only" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: local-only\ndescription: Only this project has it.\n---\nBe local.\n",
        encoding="utf-8",
    )
    (repo / ".git").mkdir()
    monkeypatch.chdir(repo)
    config = load_config()
    config.fleet.roles["tester"] = FleetRoleSettings(persona="ghost")
    save_config(config)

    async def scenario(pilot: Pilot[None], host: AppHost) -> tuple[list[str], list[str]]:
        await pilot.pause()
        return _persona_prompts(host, "coder"), _persona_prompts(host, "tester")

    coder, tester = drive(scenario)
    assert coder[0] == "(none)" and "skeptic" in coder
    assert "local-only" not in coder, "a project's own persona is not a machine-wide default"
    assert tester[-1] == "ghost (custom)"


def test_the_form_says_why_it_lists_no_personas_when_they_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unreadable(root: Path | None = None) -> Any:
        raise PermissionError(13, "Permission denied", "/h/.aisquare/personas")

    monkeypatch.setattr("aisquare.core.personas.catalogue", unreadable)

    async def scenario(pilot: Pilot[None], host: AppHost) -> tuple[bool, str, list[str]]:
        await pilot.pause()
        note = host.query_one("#settings-personas-unavailable", Static)
        return bool(note.display), str(note.render()), _persona_prompts(host, "coder")

    shown, text, prompts = drive(scenario)
    assert shown
    assert text == (
        "personas unavailable — PermissionError: [Errno 13] Permission denied: "
        "'/h/.aisquare/personas'"
    )
    assert prompts == ["(none)"]  # no names, and the form is still up


# --- role bindings (#98) -----------------------------------------------------------------


def _bind_cli(*argv: str) -> None:
    from typer.testing import CliRunner

    from aisquare.cli.app import app

    result = CliRunner().invoke(app, ["team", "bind", *argv])
    assert result.exit_code == 0, result.output


def _after_cli(*argv: str) -> bytes:
    """The bytes ``team bind <argv>`` would leave, the file put back as it was after."""
    before = paths.config_path().read_bytes()
    _bind_cli(*argv)
    after = paths.config_path().read_bytes()
    paths.config_path().write_bytes(before)
    return after


def test_role_bindings_list_each_binding_with_its_claude_dirs_called_out() -> None:
    """#98: nothing in the UI showed a binding. Each bound role is listed with its binary,
    its args, CLAUDE_CONFIG_DIR and CLAUDE_CODE_TMPDIR with their values, and any other
    variable by name only, since its value may be a token."""
    _bind_cli(
        "coder",
        "--bin",
        "claude2",
        "--env",
        "CLAUDE_CONFIG_DIR=/srv/claude-2",
        "--env",
        "CLAUDE_CODE_TMPDIR=/srv/tmp",
        "--env",
        "GH_TOKEN=ghp_secret",
        "--arg",
        "--model",
        "--arg",
        "opus",
    )

    async def scenario(pilot: Pilot[None], host: AppHost) -> str:
        await settle(pilot)
        row = host.query_one("#binding-row-coder")
        return "\n".join(str(widget.render()) for widget in row.query(Static))

    shown = drive(scenario)
    assert "coder" in shown and "claude2" in shown
    assert "CLAUDE_CONFIG_DIR = /srv/claude-2" in shown
    assert "CLAUDE_CODE_TMPDIR = /srv/tmp" in shown
    assert "GH_TOKEN" in shown and "ghp_secret" not in shown
    assert "args --model opus" in shown


def test_no_binding_shows_how_to_make_one() -> None:
    async def scenario(pilot: Pilot[None], host: AppHost) -> str:
        await settle(pilot)
        return str(host.query_one("#bindings-empty", Static).render())

    assert "aisquare team bind" in drive(scenario)


def test_edit_opens_the_bind_form_on_the_binding_and_writes_what_team_bind_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Edit is the bind form on the binding as it is; Save replaces it, exactly as
    `team bind --clear` then `team bind` with the new values would."""
    from textual.widgets import TextArea

    from aisquare.cli.ui.attach import MASKED, NewBindScreen

    monkeypatch.setattr(
        "aisquare.cli.ui.attach.shutil.which", lambda name: f"/usr/local/bin/{name}"
    )
    _bind_cli("coder", "--bin", "claude2", "--env", "A=1", "--arg", "--model", "--arg", "opus")
    expected = _after_cli("coder", "--clear")
    paths.config_path().write_bytes(expected)
    expected = _after_cli("coder", "--bin", "claude2", "--env", "B=3", "--arg", "-v")
    paths.config_path().unlink()
    _bind_cli("coder", "--bin", "claude2", "--env", "A=1", "--arg", "--model", "--arg", "opus")

    async def scenario(pilot: Pilot[None], host: AppHost) -> dict[str, object]:
        await settle(pilot)
        host.query_one("#binding-edit-coder", Button).press()
        await settle(pilot)
        screen = host.screen
        seen: dict[str, object] = {"form": isinstance(screen, NewBindScreen)}
        seat = screen.query_one("#bind-seat", Input)
        seen["prefilled"] = (
            seat.value,
            seat.disabled,
            screen.query_one("#bind-binary", Input).value,
            screen.query_one("#bind-env", TextArea).text,
            screen.query_one("#bind-args", Input).value,
        )
        screen.query_one("#bind-env", TextArea).load_text("B=3")
        screen.query_one("#bind-args", Input).value = "-v"
        await settle(pilot)
        screen.query_one("#bind-save", Button).press()
        await settle(pilot)
        seen["shown"] = str(host.query_one("#binding-row-coder").query(Static).first().render())
        return seen

    seen = drive(scenario)
    assert seen["form"] is True
    assert seen["prefilled"] == ("coder", True, "claude2", f"A={MASKED}", "--model opus")
    assert paths.config_path().read_bytes() == expected
    assert "args -v" in str(seen["shown"]), "the section shows what the file holds now"


def test_unset_key_and_clear_write_what_team_bind_writes() -> None:
    from aisquare.cli.ui.views.app_settings import ConfirmClearBinding, UnsetKeyScreen

    _bind_cli("tester", "--env", "A=1", "--env", "B=2", "--arg", "-v")
    after_unset = _after_cli("tester", "--unset", "B")

    async def scenario(pilot: Pilot[None], host: AppHost) -> dict[str, object]:
        seen: dict[str, object] = {}
        await settle(pilot)
        host.query_one("#binding-unset-tester", Button).press()
        await settle(pilot)
        seen["asked"] = isinstance(host.screen, UnsetKeyScreen)
        host.screen.query_one("#unset-key", Select).value = "B"
        host.screen.query_one("#unset-confirm", Button).press()
        await settle(pilot)
        seen["unset"] = paths.config_path().read_bytes()
        seen["after_clear"] = _after_cli("tester", "--clear")
        host.query_one("#binding-clear-tester", Button).press()
        await settle(pilot)
        seen["confirm"] = isinstance(host.screen, ConfirmClearBinding)
        host.screen.query_one("#clear-cancel", Button).press()
        await settle(pilot)
        seen["cancelled"] = paths.config_path().read_bytes()
        host.query_one("#binding-clear-tester", Button).press()
        await settle(pilot)
        host.screen.query_one("#clear-confirm", Button).press()
        await settle(pilot)
        seen["cleared"] = paths.config_path().read_bytes()
        seen["empty"] = bool(host.query("#bindings-empty"))
        return seen

    seen = drive(scenario)
    assert seen["asked"] is True and seen["unset"] == after_unset
    assert seen["confirm"] is True and seen["cancelled"] == after_unset, "Cancel writes nothing"
    assert seen["cleared"] == seen["after_clear"]
    assert seen["empty"] is True


def test_the_role_bindings_section_fits_80_columns() -> None:
    """The fleet UI runs in an 80-column tmux pane as often as not: a binding's row, its
    called-out paths and its three buttons stay inside it."""
    _bind_cli("reviewer", "--env", "CLAUDE_CONFIG_DIR=/srv/claude-accounts/2", "--arg", "-v")

    async def run() -> tuple[int, list[int]]:
        host = AppHost()
        async with host.run_test(size=(80, 40)) as pilot:
            await settle(pilot)
            row = host.query_one("#binding-row-reviewer")
            buttons = [button.region.right for button in row.query(Button)]
            return row.region.right, buttons

    right, buttons = asyncio.run(run())
    assert right <= 80 and buttons and max(buttons) <= 80, (right, buttons)


def _binding_shown(host: App[Any], role: str) -> str | None:
    """What the section shows for ``role``'s binding, or ``None`` when it lists none."""
    rows = host.query(f"#binding-row-{role}")
    if not rows:
        return None
    return "\n".join(str(widget.render()) for widget in rows.first().query(Static))


def test_reload_shows_the_bindings_a_shell_wrote_meanwhile() -> None:
    """Review of #334 (reviewer-a's walk): Reload re-read every field but the bindings, so
    a role bound in a shell never showed, and a key unset there stayed listed."""

    async def scenario(pilot: Pilot[None], host: AppHost) -> tuple[str | None, str | None]:
        await settle(pilot)
        settings_service.bind_role("coder", env={"GH_TOKEN": "x"}, args=["-v"])
        host.query_one("#reload-settings", Button).press()
        await settle(pilot)
        bound = _binding_shown(host, "coder")
        settings_service.bind_role("coder", unset=["GH_TOKEN"])
        host.query_one("#reload-settings", Button).press()
        await settle(pilot)
        return bound, _binding_shown(host, "coder")

    bound, unset = drive(scenario)
    assert bound is not None and "GH_TOKEN" in bound
    assert unset is not None and "GH_TOKEN" not in unset


def test_edit_opens_on_the_binding_the_file_holds_now_not_the_one_last_shown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Save writes the edited binding whole, so an Edit opened on what the section last
    read wrote a shell's later `team bind` away (review of #334). The page sits open
    beside the shell in a tmux split, shown all along: only Edit itself can read it."""
    monkeypatch.setattr(
        "aisquare.cli.ui.attach.shutil.which", lambda name: f"/usr/local/bin/{name}"
    )
    settings_service.bind_role("coder", args=["-v"])

    async def scenario(pilot: Pilot[None], host: AppHost) -> tuple[str, list[str]]:
        await settle(pilot)
        settings_service.bind_role("coder", args=["--debug"])  # the shell, meanwhile
        host.query_one("#binding-edit-coder", Button).press()
        await settle(pilot)
        prefilled = host.screen.query_one("#bind-args", Input).value
        host.screen.query_one("#bind-save", Button).press()
        await settle(pilot)
        return prefilled, load_config().team.profiles["coder"].args

    prefilled, saved = drive(scenario)
    assert prefilled == "-v --debug"
    assert saved == ["-v", "--debug"], "the shell's arg survives an Edit and Save"


def test_edit_of_a_binding_cleared_meanwhile_says_so_and_opens_nothing() -> None:
    settings_service.bind_role("coder", args=["-v"])

    async def scenario(pilot: Pilot[None], host: AppHost) -> tuple[bool, str | None]:
        await settle(pilot)
        settings_service.clear_role_binding("coder")
        host.query_one("#binding-edit-coder", Button).press()
        await settle(pilot)
        return host.screen is not host.query_one(AppSettingsView).screen, _binding_shown(
            host, "coder"
        )

    opened, shown = drive(scenario)
    assert opened is False and shown is None


def test_edit_masks_every_env_value_until_show_values_and_keeps_one_left_masked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The section names GH_TOKEN and never shows its value, and Edit showed it in clear
    (review of #334). Each value is masked until *Show values*; a masked value saves as
    the one stored, and a value typed over its mask saves as typed."""
    from textual.widgets import TextArea

    from aisquare.cli.ui.attach import MASKED

    monkeypatch.setattr(
        "aisquare.cli.ui.attach.shutil.which", lambda name: f"/usr/local/bin/{name}"
    )
    settings_service.bind_role("coder", env={"GH_TOKEN": "ghp_secret", "A": "1"})

    async def scenario(pilot: Pilot[None], host: AppHost) -> list[str]:
        await settle(pilot)
        host.query_one("#binding-edit-coder", Button).press()
        await settle(pilot)
        area = host.screen.query_one("#bind-env", TextArea)
        reveal = host.screen.query_one("#bind-env-reveal", Switch)
        seen = [area.text]
        reveal.value = True
        await settle(pilot)
        seen.append(area.text)
        reveal.value = False
        await settle(pilot)
        seen.append(area.text)
        area.load_text(f"GH_TOKEN={MASKED}\nA=2")
        await settle(pilot)
        host.screen.query_one("#bind-save", Button).press()
        await settle(pilot)
        return seen

    masked, shown, again = drive(scenario)
    assert masked == again == f"GH_TOKEN={MASKED}\nA={MASKED}"
    assert shown == "GH_TOKEN=ghp_secret\nA=1"
    assert load_config().team.profiles["coder"].env == {"GH_TOKEN": "ghp_secret", "A": "2"}


def test_the_keyboard_reaches_new_bind_opens_the_form_and_escape_closes_it() -> None:
    """Step 6 of the PR's walk, which the tmux walk could not prove end to end: at 80
    columns, Tab reaches *+ New bind*, Enter opens the bind form, and Escape closes it
    having written nothing."""
    from aisquare.cli.ui.attach import NewBindScreen

    async def run() -> tuple[str | None, bool, bool]:
        host = AppHost()
        async with host.run_test(size=(80, 40)) as pilot:
            await settle(pilot)
            for _ in range(120):
                await pilot.press("tab")
                if host.focused is not None and host.focused.id == "bindings-new":
                    break
            reached = host.focused.id if host.focused is not None else None
            await pilot.press("enter")
            await settle(pilot)
            opened = isinstance(host.screen, NewBindScreen)
            await pilot.press("escape")
            await settle(pilot)
            return reached, opened, isinstance(host.screen, NewBindScreen)

    reached, opened, still_open = asyncio.run(run())
    assert reached == "bindings-new"
    assert opened is True and still_open is False
    assert not paths.config_path().exists(), "Escape writes nothing"
