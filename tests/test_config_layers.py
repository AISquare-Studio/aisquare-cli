"""Settings are two layers: the system defaults, and a user file holding only what changed.

The human's words: "an internal system recommended settings things pre set, and a user
setting config which will be empty unless user makes a change and saves it, it will use
the same key … so that users preference overrides system, if user does nothing he
always gets system settings". Up to version 1 every save wrote every setting out, so a user's
choice could not be told from a default, and a default that moved on stayed pinned in
every file a save had touched.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import tomli_w

from aisquare.core.config import (
    _V1_DEFAULTS,
    CONFIG_VERSION,
    REVIEWER_ARGS,
    AccountsSettings,
    AppConfig,
    FleetRoleSettings,
    RoleLaunchProfile,
    _overlay,
    load_config,
    save_config,
    system_defaults,
)
from aisquare.core.paths import config_path
from aisquare.services import settings as settings_service


def _file(target: Path) -> dict[str, Any]:
    return tomllib.loads(target.read_text(encoding="utf-8"))


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


def test_a_config_at_every_default_writes_nothing_but_its_version(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    save_config(AppConfig(), target)
    assert _file(target) == {"config_version": CONFIG_VERSION}


def test_a_save_writes_only_the_keys_that_differ_from_the_system_defaults(
    tmp_path: Path,
) -> None:
    """Nested: one key of one role is the role's table with that key, not the role in full,
    and not the five other roles beside it."""
    target = tmp_path / "config.toml"
    config = AppConfig()
    config.fleet.roles["coder"].permission_mode = "acceptEdits"
    config.accounts.pick = "default"
    config.snapshot.ignore = ["**/fixtures/**"]
    save_config(config, target)
    assert _file(target) == {
        "config_version": CONFIG_VERSION,
        "fleet": {"roles": {"coder": {"permission_mode": "acceptEdits"}}},
        "accounts": {"pick": "default"},
        "snapshot": {"ignore": ["**/fixtures/**"]},
    }
    assert load_config(target) == config


def test_setting_a_key_back_to_its_default_takes_it_out_of_the_file(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    config = AppConfig()
    config.fleet.max_agents_per_project = 6
    save_config(config, target)
    assert _file(target)["fleet"] == {"max_agents_per_project": 6}
    config.fleet.max_agents_per_project = 4
    save_config(config, target)
    assert "fleet" not in _file(target)


def test_config_set_writes_the_one_key_it_was_given() -> None:
    """Through the command a user runs, into the standard location."""
    settings_service.set_value("fleet.escape_key", "f11")
    assert _file(config_path()) == {
        "config_version": CONFIG_VERSION,
        "fleet": {"escape_key": "f11"},
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
    assert written["fleet"] == {"roles": {"designer": {}}}
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
        "fleet": {"roles": {"coder": {"persona": "terse"}}},
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


# --- version 1 -> 2 --------------------------------------------------------------------


def test_the_v1_snapshot_differs_from_today_only_by_the_headroom_defaults() -> None:
    """The snapshot is version 1's defaults, frozen. When a default changes later, it
    changes here — never in ``_V1_DEFAULTS``, which is what 0.8 wrote."""
    changed = {"accounts": {"pick": "headroom", "on_limit": "switch"}}
    assert _overlay(_V1_DEFAULTS, changed) == system_defaults()


def test_a_v1_file_keeps_only_what_differs_from_v1s_defaults(tmp_path: Path) -> None:
    """Every value 0.8 wrote because it was the default follows today's: ``pick`` and
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
        "fleet": {"roles": {"coder": {"permission_mode": "acceptEdits"}}},
        "team": {"profiles": {"coder": {"bin": "claude2"}}},
        "future_feature": {"something": 42},
    }


def test_a_v1_file_that_chose_headroom_keeps_it(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    _v1_file(target, accounts={"pick": "headroom", "on_limit": "switch"})
    loaded = load_config(target)
    assert loaded.accounts.pick == "headroom" and loaded.accounts.on_limit == "switch"
    save_config(loaded, target)
    assert _file(target) == {"config_version": CONFIG_VERSION}  # the defaults now


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
    assert _file(target) == {"config_version": CONFIG_VERSION}
