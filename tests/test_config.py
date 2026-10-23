"""Config defaults, round-trips, and the home directory layout."""

from __future__ import annotations

import codecs
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import snapshot
from aisquare.core.config import (
    CONFIG_VERSION,
    REVIEWER_ARGS,
    AppConfig,
    CaptureSettings,
    ExperimentSettings,
    FleetSettings,
    RedactionSettings,
    SnapshotSettings,
    load_config,
    save_config,
)
from aisquare.core.paths import aisquare_home, cache_dir, config_path, ensure_home, log_dir
from aisquare.models import RedactionLevel
from aisquare.services import settings as settings_service


def test_load_missing_file_returns_defaults() -> None:
    assert load_config() == AppConfig()


def test_the_file_is_parsed_again_only_when_its_bytes_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 18: asq's refresh tick asked config.toml for the fleet's, the
    roles', the accounts' and the capture setting, and each ask parsed and validated the
    whole file; only ``capture`` had a memo. ``load_config`` keeps its last parse, keyed by
    the file's bytes: a rewrite in place to the same size, with the same inode and mtime as
    a write within one tick of the kernel's coarse clock has, is read for what it says."""
    import os

    from aisquare.core import config as config_core

    real = config_core._parse_toml
    parses: list[bytes] = []

    def counted(raw: bytes) -> dict[str, object]:
        parses.append(raw)
        return real(raw)

    monkeypatch.setattr(config_core, "_parse_toml", counted)
    path = tmp_path / "config.toml"
    path.write_text("[fleet]\nmax_agents_per_project = 4\n", encoding="utf-8")

    assert [load_config(path).fleet.max_agents_per_project for _ in range(3)] == [4, 4, 4]
    assert len(parses) == 1, "parsed once"

    seen = path.stat()
    with path.open("r+b") as handle:  # in place: the same inode
        handle.write(b"[fleet]\nmax_agents_per_project = 5\n")
    os.utime(path, ns=(seen.st_atime_ns, seen.st_mtime_ns))
    again = path.stat()
    assert (again.st_ino, again.st_size, again.st_mtime_ns) == (
        seen.st_ino,
        seen.st_size,
        seen.st_mtime_ns,
    )
    assert load_config(path).fleet.max_agents_per_project == 5
    assert len(parses) == 2, "and again once its bytes changed"


def test_each_load_hands_out_its_own_config(tmp_path: Path) -> None:
    """The kept parse is never handed out: a caller changes what it loaded and saves it, and a
    change it did not save is not what the next load reads."""
    path = tmp_path / "config.toml"
    path.write_text("[fleet]\nmax_agents_per_project = 4\n", encoding="utf-8")

    first = load_config(path)
    first.fleet.max_agents_per_project = 9  # a nested model: a shallow copy would share it

    second = load_config(path)
    assert second.fleet.max_agents_per_project == 4
    assert second is not first and second.fleet is not first.fleet


def test_a_reader_that_only_reads_is_handed_the_kept_parse_uncopied(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 19: every load deep-copied the kept parse, so ``capture_on()``
    copied the whole model for one bool, several times a session start and on asq's every
    refresh tick. A reader that only reads (``copy=False``) is handed the kept parse; a
    caller that may change what it loads still gets its own."""
    from aisquare.core import config as config_core
    from aisquare.services import project as project_service

    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[projects]\ncapture = true\n", encoding="utf-8")
    real = AppConfig.model_copy
    copies: list[int] = []

    def counted(self: AppConfig, *args: object, **kwargs: object) -> AppConfig:
        copies.append(1)
        return real(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(config_core.AppConfig, "model_copy", counted)

    assert [project_service.capture_on() for _ in range(3)] == [True, True, True]
    assert copies == [], "copied for a read"
    assert load_config(path, copy=False) is load_config(path, copy=False)
    mine = load_config(path)
    assert mine is not load_config(path, copy=False) and copies


def test_a_read_only_load_in_one_load_never_shares_the_kept_parse(tmp_path: Path) -> None:
    """Review of #259, round 20: ``one_load`` keeps what the first load in its block returned
    and hands it to every later load there, ``copy=True`` ones too. A ``copy=False`` load
    first (``capture_on()``) put the kept parse itself in it; a caller later in the block that
    changed its config changed the kept parse, and every load of the same bytes, in any
    caller, then had the change until the file's bytes changed."""
    from aisquare.core.config import one_load

    path = tmp_path / "config.toml"
    path.write_text("[fleet]\nmax_agents_per_project = 4\n", encoding="utf-8")

    with one_load():
        load_config(path, copy=False)
        mine = load_config(path)
        mine.fleet.max_agents_per_project = 9  # a change it never saves

    assert load_config(path, copy=False).fleet.max_agents_per_project == 4
    assert load_config(path).fleet.max_agents_per_project == 4


def test_a_file_that_will_not_parse_is_read_again_each_load(tmp_path: Path) -> None:
    """Nothing is kept of a load that raised: the next load reads the file again, and a
    broken file after a good one raises rather than handing out the good one."""
    path = tmp_path / "config.toml"
    path.write_text("[fleet\n", encoding="utf-8")
    with pytest.raises(tomllib.TOMLDecodeError):
        load_config(path)
    path.write_text("[fleet]\nmax_agents_per_project = 4\n", encoding="utf-8")
    assert load_config(path).fleet.max_agents_per_project == 4
    path.write_text("[fleet\n", encoding="utf-8")
    with pytest.raises(tomllib.TOMLDecodeError):
        load_config(path)


def test_an_unknown_key_in_the_file_still_loads(tmp_path: Path) -> None:
    # The safety net under every key this schema has ever dropped, and the
    # reason `team.bins` could simply be deleted instead of deprecated. Extras
    # are ignored by default in pydantic, which makes it an accident waiting to
    # be configured away — a later `extra="forbid"` would turn one stale line in
    # a hand-written config into a CLI that cannot start at all.
    target = tmp_path / "config.toml"
    target.write_text(
        'profile = "work"\n\n[team.bins]\ncoder = "claude2"\n\n[nonsense]\nx = 1\n',
        encoding="utf-8",
    )
    config = load_config(target)
    assert config.profile == "work"
    assert config.team.profiles == {}


def test_a_config_saved_with_a_utf8_bom_loads_and_a_save_keeps_its_unknown_keys(
    tmp_path: Path,
) -> None:
    """Windows PowerShell 5.1's ``Set-Content -Encoding UTF8`` and Notepad's "UTF-8 with
    BOM" put U+FEFF in front, which ``tomllib`` refuses: every command raised, and a
    save could not read the file it merges into, so a section this build does not know
    was dropped (review of the #203 store fixes, round 1). Read past the BOM, the file
    loads, a save keeps the section, and the file is written back without the BOM."""
    target = tmp_path / "config.toml"
    body = 'profile = "work"\n\n[future_feature]\nsomething = 42\n'
    target.write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))

    config = load_config(target)
    assert config.profile == "work"
    save_config(config, target)

    raw = target.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    written = tomllib.loads(raw.decode("utf-8"))
    assert written["profile"] == "work"
    assert written["future_feature"] == {"something": 42}


@pytest.mark.parametrize("bom", [codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE], ids=["le", "be"])
def test_a_config_saved_as_utf16_loads_and_a_save_writes_it_as_utf8(
    tmp_path: Path, bom: bytes
) -> None:
    """Windows PowerShell 5.1's ``>`` and ``Out-File`` write UTF-16 with its byte-order
    mark. ``load_config`` refused such a file, so every command failed on it, and ``init
    --reinit`` could not see the explainability section that should have refused the
    reset (review of the #203 final-review fixes). Read in the encoding its mark names, it
    loads; a save keeps what this build does not know and writes the file as UTF-8."""
    target = tmp_path / "config.toml"
    body = 'profile = "work"\n\n[future_feature]\nsomething = 42\n'
    codec = "utf-16-le" if bom == codecs.BOM_UTF16_LE else "utf-16-be"
    target.write_bytes(bom + body.encode(codec))

    config = load_config(target)
    assert config.profile == "work"
    save_config(config, target)

    written = tomllib.loads(target.read_bytes().decode("utf-8"))
    assert written["profile"] == "work"
    assert written["future_feature"] == {"something": 42}


@pytest.mark.parametrize(
    "raw",
    [b"this is not [ valid toml\n", 'profile = "work"\n'.encode("utf-16-le")],
    ids=["invalid-toml", "utf-16-without-a-mark"],
)
def test_a_save_never_writes_over_a_config_it_cannot_read_unless_told_to(
    tmp_path: Path, raw: bytes
) -> None:
    """A save keeps what the file holds that the model does not, so a file it cannot read
    is one whose contents it would lose. It failed open and wrote the model over it, which
    is how ``init --reinit`` replaced a UTF-16 cutover without its refusal (review of the
    #203 final-review fixes). The read error is raised and the file left, unless the
    caller says to discard it, as ``init --reinit --yes`` does."""
    target = tmp_path / "config.toml"
    target.write_bytes(raw)

    with pytest.raises((tomllib.TOMLDecodeError, UnicodeDecodeError)):
        save_config(AppConfig(profile="home"), target)
    assert target.read_bytes() == raw

    save_config(AppConfig(profile="home"), target, discard_unreadable=True)

    assert load_config(target).profile == "home"


def test_round_trip_explicit_path(tmp_path: Path) -> None:
    config = AppConfig(
        profile="work",
        default_pool="user",
        capture=CaptureSettings(enabled=False),
        redaction=RedactionSettings(level=RedactionLevel.strict),
    )
    target = save_config(config, tmp_path / "nested" / "config.toml")
    assert target.is_file()
    assert load_config(target) == config


def test_round_trip_default_location(isolated_home: Path) -> None:
    config = AppConfig(profile="laptop")
    target = save_config(config)
    assert target == isolated_home / "config.toml"
    assert target == config_path()
    assert load_config() == config


def test_ensure_home_creates_layout(isolated_home: Path) -> None:
    home = ensure_home()
    assert home == isolated_home
    assert home == aisquare_home()
    assert cache_dir().is_dir()
    assert log_dir().is_dir()


# --- [snapshot] max_tokens (#82) -----------------------------------------------------------


def test_snapshot_budget_defaults_to_the_packers_constant() -> None:
    """The knob exists, and its default IS the 150 000 the packer used to hardcode.

    Nobody who has not set it sees a change: ``core.snapshot.MAX_TOKENS`` is now
    read off this default rather than the other way round, so the two cannot
    drift.
    """
    assert AppConfig().snapshot == SnapshotSettings(max_tokens=150_000)
    assert AppConfig().snapshot.max_tokens == snapshot.MAX_TOKENS


def test_snapshot_budget_loads_from_the_file_and_round_trips(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text("[snapshot]\nmax_tokens = 300000\n", encoding="utf-8")
    loaded = load_config(target)
    assert loaded.snapshot.max_tokens == 300_000
    save_config(loaded, target)
    assert load_config(target).snapshot.max_tokens == 300_000
    assert "[snapshot]" in target.read_text(encoding="utf-8")


def test_config_set_writes_the_snapshot_budget_and_rejects_a_non_number(
    runner: CliRunner,
) -> None:
    """The command the failure message names, typed the way it names it."""
    result = runner.invoke(app, ["config", "set", "snapshot.max_tokens", "300000"])
    assert result.exit_code == 0, result.output
    assert load_config().snapshot.max_tokens == 300_000
    shown = runner.invoke(app, ["config", "get", "snapshot.max_tokens"])
    assert shown.stdout.strip() == "snapshot.max_tokens = 300000"

    rejected = runner.invoke(app, ["config", "set", "snapshot.max_tokens", "lots"])
    assert rejected.exit_code != 0
    assert load_config().snapshot.max_tokens == 300_000, "a rejected value must not land"


def test_config_set_takes_the_snapshot_ignore_list_comma_separated(runner: CliRunner) -> None:
    """A list key is settable from a shell at all — comma-separated, the way repomix's own flag is.

    Whitespace around items is trimmed, ``get`` prints the same shape ``set``
    accepts, and an empty string clears the list.
    """
    assert AppConfig().snapshot.ignore == []
    result = runner.invoke(
        app, ["config", "set", "snapshot.ignore", "**/fixtures/**, docs/generated/**"]
    )
    assert result.exit_code == 0, result.output
    assert load_config().snapshot.ignore == ["**/fixtures/**", "docs/generated/**"]
    shown = runner.invoke(app, ["config", "get", "snapshot.ignore"])
    assert shown.stdout.strip() == "snapshot.ignore = **/fixtures/**,docs/generated/**"

    cleared = runner.invoke(app, ["config", "set", "snapshot.ignore", ""])
    assert cleared.exit_code == 0, cleared.output
    assert load_config().snapshot.ignore == []


def test_config_get_and_set_cover_the_ui_preference(runner: CliRunner) -> None:
    """``[ui] always_show_project_path`` — App settings ▸ User preferences — is off by
    default, and ``config get`` and ``config set`` reach it as they reach every other key."""
    assert AppConfig().ui.always_show_project_path is False
    shown = runner.invoke(app, ["config", "get", "ui.always_show_project_path"])
    assert shown.stdout.strip() == "ui.always_show_project_path = false"

    result = runner.invoke(app, ["config", "set", "ui.always_show_project_path", "true"])
    assert result.exit_code == 0, result.output
    assert load_config().ui.always_show_project_path is True
    assert tomllib.loads(config_path().read_text(encoding="utf-8"))["ui"] == {
        "always_show_project_path": True
    }

    rejected = runner.invoke(app, ["config", "set", "ui.always_show_project_path", "maybe"])
    assert rejected.exit_code != 0
    assert load_config().ui.always_show_project_path is True, "a rejected value must not land"


def test_experiment_is_off_by_default() -> None:
    """The state every existing user is in. ``prompt_submit`` runs
    synchronously in front of a developer who has just hit enter, so off has to
    be the default rather than something a release turns on for everyone."""
    config = AppConfig()
    assert config.experiment.enabled is False
    assert config.experiment.url == ""


def test_the_experiment_has_no_delivery_flags_of_its_own() -> None:
    """Which hooks call the server and whether the recall tool is exposed are
    decided by the server's delivery descriptor. A client-side flag would be a
    second place the experiment's shape lives, and the two would disagree."""
    for flag in ("push", "pull", "arm", "architecture"):
        assert flag not in ExperimentSettings.model_fields


def test_experiment_settings_round_trip(tmp_path: Path) -> None:
    config = AppConfig(
        experiment=ExperimentSettings(enabled=True, url="http://ci.internal", run="run_kernel0001")
    )
    target = save_config(config, tmp_path / "config.toml")
    assert load_config(target) == config
    assert load_config(target).experiment.run == "run_kernel0001"


def test_a_config_from_the_v1_branch_still_loads(tmp_path: Path) -> None:
    """``push``/``pull`` were fields once; a file that still carries them loads,
    because unknown keys are ignored — and they change nothing."""
    target = tmp_path / "config.toml"
    target.write_text("[experiment]\nenabled = true\npush = false\npull = true\n", encoding="utf-8")
    loaded = load_config(target)
    assert loaded.experiment.enabled is True
    assert loaded.experiment.run == ""


def test_experiment_has_nowhere_to_put_a_key() -> None:
    """config.toml is a file people diff, paste into issues and copy between
    machines; the bearer token is read from the environment only."""
    assert "key" not in ExperimentSettings.model_fields


def test_a_config_without_an_experiment_section_still_loads(tmp_path: Path) -> None:
    """Every config written before this release."""
    target = tmp_path / "config.toml"
    target.write_text('profile = "default"\n', encoding="utf-8")
    assert load_config(target).experiment.enabled is False


_SAVED_BY_0_7_0_REVIEWER = """\
[fleet.roles.coder]
permission_mode = "auto"
worktree = true
extra_args = []

[fleet.roles.reviewer]
permission_mode = "auto"
worktree = true
extra_args = [
    "--restricted",
]
"""


def test_a_reviewer_saved_with_the_former_restricted_default_loads_with_a_shell(
    tmp_path: Path,
) -> None:
    """0.6.0 and 0.7.0 launched the reviewer `--restricted`, which removes Bash, and any
    `config set` wrote that default into the file. A new default alone would leave
    every such file launching a reviewer that cannot run `gh` — this machine's did.
    The old default is replaced by today's, so the reviewer still cannot edit files."""
    target = tmp_path / "config.toml"
    target.write_text(_SAVED_BY_0_7_0_REVIEWER, encoding="utf-8")
    loaded = load_config(target)
    reviewer = loaded.fleet.roles["reviewer"]
    assert reviewer.extra_args == list(REVIEWER_ARGS)
    assert reviewer.worktree is True, "only the old default goes; the rest stays as saved"
    save_config(loaded, target)
    saved = tomllib.loads(target.read_text(encoding="utf-8"))
    assert saved["fleet"]["roles"]["reviewer"]["extra_args"] == list(REVIEWER_ARGS), (
        "today's default, in the reviewer's table written whole for older builds"
    )
    assert load_config(target).fleet.roles["reviewer"].extra_args == list(REVIEWER_ARGS)


def test_the_built_in_reviewer_keeps_its_shell_and_is_denied_the_tools_that_edit() -> None:
    """Bash is what `gh pr diff` and `gh pr review` run on; Edit, Write and NotebookEdit
    are what a prompt injected through the PR it reads would edit the tree with. One
    token: a separate list would swallow the next plain word on the command line."""
    reviewer = FleetSettings().roles["reviewer"]
    assert reviewer.extra_args == ["--disallowedTools=Edit Write NotebookEdit"]
    assert "--restricted" not in reviewer.extra_args


def test_the_former_reviewer_default_is_replaced_once_and_chosen_afterwards_is_kept(
    tmp_path: Path,
) -> None:
    """Replaced on every load, `["--restricted"]` could not be chosen: `config set` said
    it was saved, the fleet launched without it, and the next save erased it. The save
    that follows the replacement records the version it reached, and from then on the
    file's list is kept."""
    target = tmp_path / "config.toml"
    target.write_text(_SAVED_BY_0_7_0_REVIEWER, encoding="utf-8")
    loaded = load_config(target)
    assert loaded.fleet.roles["reviewer"].extra_args == list(REVIEWER_ARGS)
    save_config(loaded, target)
    assert tomllib.loads(target.read_text(encoding="utf-8"))["config_version"] == CONFIG_VERSION

    chosen = load_config(target)
    chosen.fleet.roles["reviewer"].extra_args = ["--restricted"]
    save_config(chosen, target)
    assert load_config(target).fleet.roles["reviewer"].extra_args == ["--restricted"]
    save_config(load_config(target), target)  # any later save, of any key
    assert load_config(target).fleet.roles["reviewer"].extra_args == ["--restricted"]


def test_a_config_carries_one_version_and_no_marker_of_a_migration(tmp_path: Path) -> None:
    """Round 23 of #257: every save wrote `migrated_reviewer_args = true` into `[fleet]`,
    an undocumented key, and the next change to a default would have needed another. One
    `config_version` at the top instead, with the steps in order. Only builds of this
    release candidate wrote the marker, so no release reads it (round 26)."""
    fresh = tmp_path / "fresh.toml"
    save_config(AppConfig(), fresh)
    written = fresh.read_text(encoding="utf-8")
    assert written.startswith(f"config_version = {CONFIG_VERSION}\n"), written
    assert "migrated_reviewer_args" not in written


def test_a_config_a_newer_build_wrote_keeps_its_version_and_runs_no_step(tmp_path: Path) -> None:
    """A version above this build's was written by a newer build, whose steps have run on
    the file: none runs again here, and a save from here keeps that version, so the newer
    build does not run them again on what this one wrote."""
    target = tmp_path / "config.toml"
    newer = CONFIG_VERSION + 1
    target.write_text(f"config_version = {newer}\n{_SAVED_BY_0_7_0_REVIEWER}", encoding="utf-8")

    loaded = load_config(target)
    save_config(loaded, target)

    assert loaded.fleet.roles["reviewer"].extra_args == ["--restricted"]
    assert tomllib.loads(target.read_text(encoding="utf-8"))["config_version"] == newer


def test_config_set_of_the_former_reviewer_default_takes_effect() -> None:
    """Through `config set` itself, on a 0.7.0 file: what it reports is what loads."""
    target = config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_SAVED_BY_0_7_0_REVIEWER, encoding="utf-8")
    key = "fleet.roles.reviewer.extra_args"
    assert settings_service.set_value(key, "--restricted") == "--restricted"
    assert load_config().fleet.roles["reviewer"].extra_args == ["--restricted"]


def test_the_reviewer_default_set_by_config_set_round_trips() -> None:
    """`config set` splits a list on commas and `config get` joins one with them, so the
    default's tool names are apart by spaces: what `get` prints, `set` takes back whole."""
    key = "fleet.roles.reviewer.extra_args"
    printed = settings_service.get_value(key)
    assert settings_service.set_value(key, printed) == printed
    assert load_config().fleet.roles["reviewer"].extra_args == list(REVIEWER_ARGS)


def test_a_reviewer_given_more_than_the_former_default_keeps_it(tmp_path: Path) -> None:
    """Only the exact old built-in list is the stale default; a list someone wrote is theirs."""
    target = tmp_path / "config.toml"
    target.write_text(
        _SAVED_BY_0_7_0_REVIEWER.replace('"--restricted",', '"--restricted", "--tools", "Bash"'),
        encoding="utf-8",
    )
    reviewer = load_config(target).fleet.roles["reviewer"]
    assert reviewer.extra_args == ["--restricted", "--tools", "Bash"]
