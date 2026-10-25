"""Settings are two layers: the system defaults, and a user file holding only what changed.

The human's words: "an internal system recommended settings things pre set, and a user
setting config which will be empty unless user makes a change and saves it, it will use
the same key … so that users preference overrides system, if user does nothing he
always gets system settings", and "a reset all settings to default button for wiping
user preference file". Up to version 1 every save wrote every setting out, so a user's
choice could not be told from a default, and a default that moved on stayed pinned in
every file a save had touched.
"""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest
import tomli_w
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core.config import (
    _V1_DEFAULTS,
    CONFIG_VERSION,
    REVIEWER_ARGS,
    AccountsSettings,
    AppConfig,
    FleetRoleSettings,
    RoleLaunchProfile,
    _overlay,
    holds_a_setting,
    load_config,
    overrides,
    reset_config,
    save_config,
    system_defaults,
)
from aisquare.core.paths import config_path
from aisquare.services import settings as settings_service


def _file(target: Path) -> dict[str, Any]:
    return tomllib.loads(target.read_text(encoding="utf-8"))


#: Every fleet role's table at its defaults: what every save writes whole, so that a build
#: before the layers, which reads the file without the defaults under it, sees them all.
_ROLES: dict[str, Any] = system_defaults()["fleet"]["roles"]


def _roles(**changes: Any) -> dict[str, Any]:
    """The whole role tables a save writes, with ``changes`` laid over their defaults."""
    return dict(_overlay(_ROLES, changes))


def _v1_file(target: Path, **changes: Any) -> None:
    """``target`` as version 1 saved it: every setting written out, ``changes`` laid over."""
    target.write_text(
        tomli_w.dumps({"config_version": 1, **_overlay(_V1_DEFAULTS, changes)}), encoding="utf-8"
    )


# --- the system layer ------------------------------------------------------------------


def test_use_available_headroom_is_the_default() -> None:
    """ "make use available headroom settings default going forward": a launch nothing
    names an account for takes the one with room, and an agent that hits its limit is
    moved to one."""
    accounts = AccountsSettings()
    assert accounts.pick == "headroom"
    assert accounts.on_limit == "switch"
    assert load_config().accounts == accounts  # no file: the system defaults


def test_the_system_defaults_are_the_models_own() -> None:
    """One place: the field defaults, dumped. Nothing else holds a copy to drift from."""
    assert system_defaults() == AppConfig().model_dump(mode="json", exclude_none=True)
    assert AppConfig.model_validate(system_defaults()) == AppConfig()


# --- the user layer: written sparse ----------------------------------------------------


def test_a_config_at_every_default_writes_its_version_and_the_role_tables(
    tmp_path: Path,
) -> None:
    target = tmp_path / "config.toml"
    save_config(AppConfig(), target)
    assert _file(target) == {"config_version": CONFIG_VERSION, "fleet": {"roles": _ROLES}}
    assert not holds_a_setting(target)


def test_a_save_writes_only_the_keys_that_differ_from_the_system_defaults(
    tmp_path: Path,
) -> None:
    """Nested, except the fleet's roles: every role's table is written whole, one key of
    one role changed in it (``_with_whole_role_tables`` says why)."""
    target = tmp_path / "config.toml"
    config = AppConfig()
    config.fleet.roles["coder"].permission_mode = "acceptEdits"
    config.accounts.pick = "default"
    config.snapshot.ignore = ["**/fixtures/**"]
    save_config(config, target)
    assert _file(target) == {
        "config_version": CONFIG_VERSION,
        "fleet": {"roles": _roles(coder={"permission_mode": "acceptEdits"})},
        "accounts": {"pick": "default"},
        "snapshot": {"ignore": ["**/fixtures/**"]},
    }
    assert load_config(target) == config


def test_setting_a_key_back_to_its_default_takes_it_out_of_the_file(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    config = AppConfig()
    config.fleet.max_agents_per_project = 6
    save_config(config, target)
    assert _file(target)["fleet"] == {"max_agents_per_project": 6, "roles": _ROLES}
    config.fleet.max_agents_per_project = 4
    save_config(config, target)
    assert _file(target)["fleet"] == {"roles": _ROLES}


def test_config_set_writes_the_one_key_it_was_given() -> None:
    """Through the command a user runs, into the standard location."""
    settings_service.set_value("fleet.escape_key", "f11")
    assert _file(config_path()) == {
        "config_version": CONFIG_VERSION,
        "fleet": {"escape_key": "f11", "roles": _ROLES},
    }


def test_an_entry_the_user_added_to_a_mapping_is_written_with_only_what_it_sets(
    tmp_path: Path,
) -> None:
    """A role binding's being there is itself the setting (``launch._declared_roles`` reads
    the table), so it stays even when it holds nothing but defaults — and holds no
    ``env = {}`` or ``args = []`` beside the one thing bound."""
    target = tmp_path / "config.toml"
    config = AppConfig()
    config.team.profiles["coder"] = RoleLaunchProfile(account="2")
    config.fleet.roles["designer"] = FleetRoleSettings()
    save_config(config, target)
    written = _file(target)
    assert written["team"] == {"profiles": {"coder": {"account": "2"}}}
    designer = {"permission_mode": "auto", "worktree": False, "extra_args": [], "agent_args": {}}
    assert written["fleet"] == {"roles": {**_ROLES, "designer": designer}}
    loaded = load_config(target)
    assert loaded.team.profiles == {"coder": RoleLaunchProfile(account="2")}
    assert loaded.fleet.roles["designer"] == FleetRoleSettings()


def test_a_binding_cleared_leaves_the_file(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    config = AppConfig()
    config.team.profiles["coder"] = RoleLaunchProfile(bin="claude2")
    save_config(config, target)
    config.team.profiles.pop("coder")
    save_config(config, target)
    assert "team" not in _file(target)


def test_a_key_this_build_does_not_know_survives_in_a_section_at_its_defaults(
    tmp_path: Path,
) -> None:
    """A section at every default is not written, but a newer build's key inside it — or
    inside a role at its defaults — is still kept (``_keep_unknown``)."""
    target = tmp_path / "config.toml"
    target.write_text(
        "[explainability]\nfuture = 1\n\n[fleet.roles.coder]\npersona = 'terse'\n\n"
        "[future_feature]\nsomething = 42\n",
        encoding="utf-8",
    )
    save_config(load_config(target), target)
    assert _file(target) == {
        "config_version": CONFIG_VERSION,
        "explainability": {"future": 1},
        "fleet": {"roles": _roles(coder={"persona": "terse"})},
        "future_feature": {"something": 42},
    }


# --- load: the user file over the system defaults --------------------------------------


def test_the_user_file_wins_and_whatever_it_leaves_out_is_the_default(tmp_path: Path) -> None:
    """Key by key, into a role's table too: the coder keeps its worktree, and the five
    roles the file does not name are still there with their own defaults."""
    target = tmp_path / "config.toml"
    target.write_text(
        'config_version = 2\n\n[accounts]\npick = "default"\n\n'
        '[fleet.roles.coder]\npermission_mode = "acceptEdits"\n',
        encoding="utf-8",
    )
    loaded = load_config(target)
    assert loaded.accounts.pick == "default"
    assert loaded.accounts.on_limit == "switch"
    assert loaded.fleet.roles["coder"] == FleetRoleSettings(
        permission_mode="acceptEdits", worktree=True
    )
    assert loaded.fleet.roles["reviewer"].extra_args == list(REVIEWER_ARGS)
    assert set(loaded.fleet.roles) == set(AppConfig().fleet.roles)


def test_config_list_overrides_shows_only_what_the_user_changed(runner: CliRunner) -> None:
    assert runner.invoke(app, ["config", "set", "accounts.switch_at", "70"]).exit_code == 0
    shown = runner.invoke(app, ["config", "list", "--overrides"])
    assert shown.exit_code == 0, shown.output
    assert tomllib.loads(shown.output) == {"accounts": {"switch_at": 70}}
    as_json = runner.invoke(app, ["--json", "config", "list", "--overrides"])
    assert as_json.exit_code == 0 and '"switch_at": 70' in as_json.output
    full = runner.invoke(app, ["config", "list"])
    assert tomllib.loads(full.output)["accounts"]["pick"] == "headroom"  # resolved, in full


def test_config_list_overrides_with_none_still_prints_toml(runner: CliRunner) -> None:
    shown = runner.invoke(app, ["config", "list", "--overrides"])
    assert shown.exit_code == 0
    assert tomllib.loads(shown.output) == {}
    assert "system default" in shown.output


# --- version 1 -> 2 --------------------------------------------------------------------


def test_the_v1_snapshot_differs_from_today_only_by_the_headroom_defaults_and_the_agents() -> None:
    """The snapshot is version 1's defaults, frozen. When a default changes later, or a
    setting is added, it changes here — never in ``_V1_DEFAULTS``, which is what 0.8.0 wrote."""
    changed = {
        "accounts": {
            "pick": "headroom",
            "on_limit": "switch",
            "share_trust": True,
            "share_settings": False,
            "prefer": "priority",  # added by P0-W3 (#277)
        },
        "projects": {"capture": False},
        # Added with the coding-agent adapters (#113): each agent's own role arguments (under
        # fleet, below), its model pins and whether its MCP server is wired.
        "agents": {"mcp": False, "models": {}},
        # The persona train and the captain's switch, added since (#201, #240).
        "persona": {
            "system_prompt": True,
            "import": {"engine": "auto", "api_model": "claude-opus-5"},
        },
        "experimental": {"captain": False},
        # App settings ▸ User preferences, added since.
        "ui": {"always_show_project_path": False},
        "fleet": {
            # Each role's agent arguments, per agent (#113).
            "roles": {role: {"agent_args": {}} for role in _V1_DEFAULTS["fleet"]["roles"]},
            # The first message's two (P0-W1a, #122).
            "first_prompt": "argv",
            "prompt_ack_seconds": 45.0,
            # The idle check's quiet window (P0-W1b, #123).
            "busy_quiet_seconds": 30,
            # The delivery guards (P0-W2, #276).
            "draft_quiet_seconds": 10.0,
            "deliver_notes_at_turn_end": True,
        },
    }
    assert _overlay(_V1_DEFAULTS, changed) == system_defaults()


def test_a_v1_file_keeps_only_what_differs_from_v1s_defaults(tmp_path: Path) -> None:
    """Every value 0.8.0 wrote because it was the default follows today's: ``pick`` and
    ``on_limit`` move to the new defaults. Every value the user changed stays."""
    target = tmp_path / "config.toml"
    _v1_file(
        target,
        accounts={"switch_at": 70},
        fleet={"roles": {"coder": {"permission_mode": "acceptEdits"}}},
        team={"profiles": {"coder": {"bin": "claude2", "env": {}, "args": []}}},
        future_feature={"something": 42},
    )
    loaded = load_config(target)
    assert loaded.accounts == AccountsSettings(switch_at=70)
    assert loaded.accounts.pick == "headroom" and loaded.accounts.on_limit == "switch"
    assert loaded.fleet.roles["coder"].permission_mode == "acceptEdits"
    assert loaded.team.profiles["coder"].bin == "claude2"

    save_config(loaded, target)
    assert _file(target) == {
        "config_version": CONFIG_VERSION,
        "accounts": {"switch_at": 70},
        "fleet": {"roles": _roles(coder={"permission_mode": "acceptEdits"})},
        "team": {"profiles": {"coder": {"bin": "claude2"}}},
        "future_feature": {"something": 42},
    }


def test_a_v1_file_that_chose_headroom_keeps_it(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    _v1_file(target, accounts={"pick": "headroom", "on_limit": "switch"})
    loaded = load_config(target)
    assert loaded.accounts.pick == "headroom" and loaded.accounts.on_limit == "switch"
    save_config(loaded, target)
    assert _file(target) == {"config_version": CONFIG_VERSION, "fleet": {"roles": _ROLES}}


def test_the_v1_step_runs_once_and_what_is_chosen_afterwards_is_kept(tmp_path: Path) -> None:
    """``pick = "default"`` in a version-1 file is taken for v1's default; written by a
    build of version 2, it is a choice."""
    target = tmp_path / "config.toml"
    _v1_file(target)
    assert load_config(target).accounts.pick == "headroom"
    chosen = load_config(target)
    chosen.accounts.pick = "default"
    save_config(chosen, target)
    assert load_config(target).accounts.pick == "default"
    save_config(load_config(target), target)  # any later save
    assert load_config(target).accounts.pick == "default"


def test_a_v0_file_takes_both_steps(tmp_path: Path) -> None:
    """0.7.0 wrote no version: its reviewer default is replaced by today's, which is then a
    default like any other and leaves the file."""
    target = tmp_path / "config.toml"
    v0 = _overlay(
        _V1_DEFAULTS, {"fleet": {"roles": {"reviewer": {"extra_args": ["--restricted"]}}}}
    )
    target.write_text(tomli_w.dumps(v0), encoding="utf-8")
    loaded = load_config(target)
    assert loaded.fleet.roles["reviewer"].extra_args == list(REVIEWER_ARGS)
    save_config(loaded, target)
    assert _file(target) == {"config_version": CONFIG_VERSION, "fleet": {"roles": _ROLES}}


# --- reset -----------------------------------------------------------------------------


def test_reset_moves_the_user_file_aside_and_everything_is_the_default() -> None:
    settings_service.set_value("accounts.pick", "default")
    before = config_path().read_bytes()

    backup = settings_service.reset_settings()

    assert backup is not None and backup.parent == config_path().parent
    assert backup.name.startswith("config.toml.bak-")
    assert backup.read_bytes() == before  # the one recoverable copy
    assert not config_path().exists()
    assert load_config() == AppConfig()
    assert settings_service.reset_settings() is None  # nothing left to move


def test_reset_never_overwrites_an_earlier_backup(monkeypatch: pytest.MonkeyPatch) -> None:
    from aisquare.core import config as core_config

    monkeypatch.setattr(core_config, "_BACKUP_STAMP", "same")
    settings_service.set_value("fleet.escape_key", "f11")
    first = settings_service.reset_settings()
    settings_service.set_value("fleet.escape_key", "f10")
    second = settings_service.reset_settings()
    assert first is not None and second is not None and first != second
    assert second.name == "config.toml.bak-same-2"
    assert "f11" in first.read_text(encoding="utf-8")
    assert "f10" in second.read_text(encoding="utf-8")


def test_reset_repairs_a_file_that_does_not_parse(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text("this is [not toml", encoding="utf-8")
    backup = reset_config(target)
    assert backup is not None and backup.read_text(encoding="utf-8") == "this is [not toml"
    assert load_config(target) == AppConfig()


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_reset_through_a_symlink_keeps_the_link(tmp_path: Path) -> None:
    """A dotfiles link stays a link: the file it points at is copied aside and emptied, so
    the next save writes through the link, not over it."""
    real = tmp_path / "dotfiles" / "aisquare.toml"
    real.parent.mkdir()
    real.write_text('[fleet]\nescape_key = "f11"\n', encoding="utf-8")
    real.chmod(0o600)
    link = tmp_path / "home" / "config.toml"
    link.parent.mkdir()
    link.symlink_to(real)

    backup = reset_config(link)

    assert link.is_symlink() and os.path.realpath(link) == str(real)
    assert real.read_text(encoding="utf-8") == ""
    assert load_config(link) == AppConfig()
    assert backup is not None and backup.parent == link.parent and not backup.is_symlink()
    assert 'escape_key = "f11"' in backup.read_text(encoding="utf-8")
    assert backup.stat().st_mode & 0o777 == 0o600


def test_config_reset_asks_first_and_changes_nothing_without_yes(runner: CliRunner) -> None:
    """Off a terminal — CliRunner's stdin is not one — and under --json, no --yes is a dry run."""
    settings_service.set_value("fleet.escape_key", "f11")
    dry = runner.invoke(app, ["config", "reset"])
    assert dry.exit_code == 0 and "dry run" in dry.output
    as_json = runner.invoke(app, ["--json", "config", "reset"])
    assert as_json.exit_code == 0 and '"reset": false' in as_json.output
    assert load_config().fleet.escape_key == "f11"

    done = runner.invoke(app, ["config", "reset", "--yes"])
    assert done.exit_code == 0, done.output
    assert "system default" in done.output and "config.toml.bak-" in done.output
    assert load_config().fleet.escape_key == "f12"

    again = runner.invoke(app, ["--json", "config", "reset", "--yes"])
    assert again.exit_code == 0 and '"reset": false' in again.output


def test_config_reset_at_a_terminal_asks(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aisquare.cli import config as config_cli

    settings_service.set_value("fleet.escape_key", "f11")
    monkeypatch.setattr(config_cli, "_stdin_is_a_terminal", lambda: True)
    declined = runner.invoke(app, ["config", "reset"], input="n\n")
    assert declined.exit_code == 0 and "nothing reset" in declined.output
    assert load_config().fleet.escape_key == "f11"
    accepted = runner.invoke(app, ["config", "reset"], input="y\n")
    assert accepted.exit_code == 0, accepted.output
    assert load_config().fleet.escape_key == "f12"


# --- older builds sharing ~/.aisquare (review of #261, M2 and m8) -----------------------


def _as_a_whole_file_reader_reads(target: Path) -> AppConfig:
    """``target`` as 0.8.0 and earlier read it: the file validated as it stands, with no
    defaults laid under it (their ``load_config`` had no overlay), its version set aside."""
    data = _file(target)
    data.pop("config_version", None)
    return AppConfig.model_validate(data)


def test_a_file_this_build_writes_reads_the_same_without_the_defaults_under_it(
    tmp_path: Path,
) -> None:
    """One key of one role, written sparse, read by 0.8.0 as the whole roles map: the
    reviewer launched there without --disallowedTools and every coder in the main checkout.
    Every role's table is written whole, so both builds read the same fleet."""
    target = tmp_path / "config.toml"
    config = AppConfig()
    config.fleet.roles["coder"].permission_mode = "acceptEdits"
    config.accounts.switch_at = 70
    save_config(config, target)

    older = _as_a_whole_file_reader_reads(target)
    assert older.fleet.roles == load_config(target).fleet.roles == config.fleet.roles
    assert older.fleet.roles["reviewer"].extra_args == list(REVIEWER_ARGS)
    assert older.fleet.roles["coder"] == FleetRoleSettings(
        permission_mode="acceptEdits", worktree=True
    )
    assert older.accounts.switch_at == 70  # what the user set reads the same
    assert overrides(load_config(target)) == {
        "fleet": {"roles": {"coder": {"permission_mode": "acceptEdits"}}},
        "accounts": {"switch_at": 70},
    }


def test_a_file_an_older_build_saved_whole_again_keeps_only_what_the_user_changed(
    tmp_path: Path,
) -> None:
    """An older build's save writes every setting and keeps the higher ``config_version``
    it found, so the version-1 step would never run on it again: back here, its
    ``pick = "default"`` and ``on_limit = "wait"`` read as the user's own choices. A file
    holding every setting is taken for what it is, whatever its version says."""
    target = tmp_path / "config.toml"
    whole = _overlay(_V1_DEFAULTS, {"accounts": {"switch_at": 70}})
    target.write_text(tomli_w.dumps({"config_version": 2, **whole}), encoding="utf-8")

    loaded = load_config(target)
    assert loaded.accounts == AccountsSettings(switch_at=70)
    assert overrides(loaded) == {"accounts": {"switch_at": 70}}
    save_config(loaded, target)
    assert _file(target) == {
        "config_version": CONFIG_VERSION,
        "fleet": {"roles": _ROLES},
        "accounts": {"switch_at": 70},
    }

    # Control: this build's own file at version 2, holding the old default as a choice.
    chosen = tmp_path / "chosen.toml"
    chosen.write_text('config_version = 2\n\n[accounts]\npick = "default"\n', encoding="utf-8")
    assert load_config(chosen).accounts.pick == "default"


def test_a_sparse_file_naming_profile_api_url_and_pool_keeps_the_old_defaults_chosen(
    tmp_path: Path,
) -> None:
    """A self-hosted user sets ``profile``, ``api_url`` and ``default_pool``, the three keys
    every whole save holds, then follows the CHANGELOG's advice and chooses ``on_limit =
    "wait"``: the file was taken for a whole one on every load, and both choices dropped.
    What tells a whole file is a value at a default, which this build never writes."""
    target = tmp_path / "config.toml"
    chosen = AccountsSettings(pick="default", on_limit="wait")
    config = AppConfig(
        profile="work",
        api_url="https://aisquare.example.com",
        default_pool="user",
        accounts=chosen,
    )
    save_config(config, target)
    assert {"profile", "api_url", "default_pool", "accounts"} <= set(_file(target))

    loaded = load_config(target)
    assert loaded.accounts == chosen
    assert (loaded.profile, loaded.api_url) == ("work", "https://aisquare.example.com")

    # The same settings saved whole by an older build: the defaults beside them say so.
    whole = _overlay(_V1_DEFAULTS, _file(target))
    target.write_text(tomli_w.dumps(whole), encoding="utf-8")
    again = load_config(target)
    assert again.accounts == AccountsSettings()  # version 1's defaults, taken as unchosen
    assert (again.profile, again.api_url) == ("work", "https://aisquare.example.com")


def test_reset_with_nothing_changed_moves_nothing(tmp_path: Path) -> None:
    """Every save leaves a file, at least its version and the role tables: a reset of it
    backed up nothing and said the user's settings were kept there."""
    settings_service.set_value("fleet.escape_key", "f12")  # the default: nothing changed
    before = config_path().read_bytes()
    assert not settings_service.anything_to_reset()
    assert settings_service.reset_settings() is None
    assert config_path().read_bytes() == before
    assert list(config_path().parent.glob("config.toml.bak-*")) == []


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_reset_through_a_symlink_with_nothing_changed_adds_no_backup(tmp_path: Path) -> None:
    """Through a link every reset added another empty backup and said it reset."""
    real = tmp_path / "dotfiles" / "aisquare.toml"
    real.parent.mkdir()
    real.write_text('[fleet]\nescape_key = "f11"\n', encoding="utf-8")
    link = tmp_path / "home" / "config.toml"
    link.parent.mkdir()
    link.symlink_to(real)

    assert reset_config(link) is not None  # the first reset has something to keep
    assert reset_config(link) is None  # the second, nothing
    assert len(list(link.parent.glob("config.toml.bak-*"))) == 1
    assert link.is_symlink() and real.read_text(encoding="utf-8") == ""


def test_config_reset_with_nothing_changed_says_so_and_asks_nothing(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aisquare.cli import config as config_cli

    settings_service.set_value("fleet.escape_key", "f12")
    monkeypatch.setattr(config_cli, "_stdin_is_a_terminal", lambda: True)
    said = runner.invoke(app, ["config", "reset"], input="y\n")
    assert said.exit_code == 0, said.output
    assert "nothing to reset" in said.output and "?" not in said.output  # nothing asked
    done = runner.invoke(app, ["--json", "config", "reset", "--yes"])
    assert done.exit_code == 0 and '"reset": false' in done.output
    assert list(config_path().parent.glob("config.toml.bak-*")) == []


def test_config_reset_prints_a_path_with_a_bracket_whole(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The paths are data, and the console reads no markup (``core.console._MARKUP``): a
    ``[`` in a home directory comes out whole (checked for review of #261's nit)."""
    home = tmp_path / "[work] home"
    monkeypatch.setenv("AISQUARE_HOME", str(home))
    settings_service.set_value("fleet.escape_key", "f11")
    done = runner.invoke(app, ["config", "reset", "--yes"])
    assert done.exit_code == 0, done.output
    assert f"{home}{os.sep}config.toml.bak-" in done.output.replace("\n", "")
