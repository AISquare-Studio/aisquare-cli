"""An expected config-write failure uses the CLI's error convention; a bug does not.

``config set`` with a bad key prints one line — ``✗ unknown config key: …``.
The same command, when the config is a symlink into a directory it cannot write,
printed 56 lines of Rich traceback with the useful sentence at the bottom,
wrapped mid-word. The message written to stop an operator looking at the wrong
directory was being delivered inside the thing operators skip past.

The fix is deliberately NOT a catch in ``main()``. A global handler would route
UNEXPECTED OSErrors through the tidy line too — and an unexpected OSError is a
bug, where a traceback is the correct output. Burying one costs whoever debugs
it later far more than a buried message costs an operator now. So the
translation happens at the boundary that KNOWS the failure is foreseeable: the
commands that write config.

``PermissionError`` is the whole discriminator and it is not a proxy for
"expected" — it IS the foreseeable outcome, "the resolved target is not
writable". Every other OSError keeps its traceback, and the test that proves so
is the half of this that makes the ruling mean anything.
"""

from __future__ import annotations

import errno
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from aisquare.cli import common
from aisquare.cli.app import app
from aisquare.core import paths
from aisquare.core.config import AppConfig, save_config
from aisquare.core.state import RuntimeState, set_state


def _config_symlinked_into_an_unwritable_directory(tmp_path: Path) -> Path:
    """The dotfiles shape: config.toml is a link into a directory we cannot write."""
    vault = tmp_path / "dotfiles"
    vault.mkdir()
    real = vault / "config.toml"
    save_config(AppConfig(), real)

    link = paths.config_path()
    link.parent.mkdir(parents=True, exist_ok=True)
    link.unlink(missing_ok=True)
    link.symlink_to(real)

    vault.chmod(0o500)
    return vault


@pytest.fixture
def unwritable_vault(tmp_path: Path) -> Iterator[Path]:
    vault = _config_symlinked_into_an_unwritable_directory(tmp_path)
    yield vault
    vault.chmod(0o700)


def test_config_set_reports_the_convention_not_a_traceback(
    runner: CliRunner, unwritable_vault: Path
) -> None:
    """The operator-facing half: one line, the right directory, exit 1."""
    result = runner.invoke(
        app, ["config", "set", "explainability.proxy_url", "http://127.0.0.1:9191"]
    )

    assert result.exit_code == 1
    assert "Traceback" not in result.output, result.output
    assert "✗" in result.output
    assert str(unwritable_vault) in result.output, (
        "the operator must be pointed at the directory that actually needs permission"
    )


def test_explainability_enable_reports_the_same_convention(
    runner: CliRunner, unwritable_vault: Path
) -> None:
    """Measured separately, because one command covering it does not cover the other.

    This is also the command that must not gain new ways to fail: it does not
    fail more often here, it fails legibly.
    """
    result = runner.invoke(app, ["explainability", "enable", "--target", "stg"])

    assert result.exit_code == 1
    assert "Traceback" not in result.output, result.output
    assert "✗" in result.output


def test_the_json_surface_carries_a_machine_readable_error(
    runner: CliRunner, unwritable_vault: Path
) -> None:
    """``--json`` is what a cutover gets scripted against; a traceback is not JSON."""
    result = runner.invoke(app, ["--json", "config", "set", "explainability.proxy_url", "http://x"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["error"]
    assert str(unwritable_vault) in json.dumps(payload)


def test_an_unexpected_oserror_still_raises_with_its_traceback(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The half a global handler would destroy, and therefore the point.

    An EIO out of the write path is not a foreseeable operator state, it is a
    bug or a failing disk. Tidying it into ``✗`` would hide a real defect behind
    a reassuring one-liner — the inverse of the harm this file fixes.
    """
    from aisquare.services import settings as settings_service

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(settings_service, "save_config", _boom)

    result = runner.invoke(app, ["config", "set", "explainability.proxy_url", "http://x"])

    assert isinstance(result.exception, OSError), (
        "an unexpected OSError must reach the operator as a crash, not as a tidy line"
    )
    assert result.exception.errno == errno.EIO
    assert "✗" not in result.output


def _missing_target_error(directory: Path) -> FileNotFoundError:
    """The exception ``save_config`` raises when a followed link's directory is gone.

    Constructed to the shape of its raise site — ``FileNotFoundError(ENOENT,
    <message naming the directory and both remedies>, <directory>)`` — because
    that guard lives in a sibling branch and this file must gate green on its
    own. The two halves meet in the composed tree: their tests pin that
    ``save_config`` raises this, these pin that the CLI routes it.
    """
    return FileNotFoundError(
        errno.ENOENT,
        f"config.toml is a symlink to {directory}/config.toml, but its directory "
        f"{directory} does not exist. Following the link is deliberate; creating a "
        f"directory tree there is not. Clone or create {directory}, or repoint the link.",
        str(directory),
    )


def test_a_missing_symlink_target_reports_the_convention(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The most foreseeable state on the dotfiles path, and it was the ugliest.

    An unwritable dotfiles directory is unusual. A dotfiles link whose target is
    not cloned YET is the ordinary state on a fresh machine: symlink the config,
    clone the repo second, and the first command that writes config lands here.
    """
    from aisquare.services import settings as settings_service

    missing = tmp_path / "dotfiles"
    monkeypatch.setattr(
        settings_service,
        "save_config",
        lambda *_a, **_k: (_ for _ in ()).throw(_missing_target_error(missing)),
    )

    result = runner.invoke(app, ["config", "set", "explainability.proxy_url", "http://x"])

    assert result.exit_code == 1
    assert "Traceback" not in result.output, result.output
    assert "✗" in result.output
    assert str(missing) in result.output, "the operator must be told which directory is missing"
    assert "repoint the link" in result.output, "the remedy save_config wrote must survive routing"


def test_explainability_enable_routes_a_missing_target_too(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Measured separately: one boundary covering it does not cover another."""
    from aisquare.cli import explainability as explainability_cli

    missing = tmp_path / "dotfiles"
    monkeypatch.setattr(
        explainability_cli,
        "save_config",
        lambda *_a, **_k: (_ for _ in ()).throw(_missing_target_error(missing)),
    )

    result = runner.invoke(app, ["explainability", "enable", "--target", "stg"])

    assert result.exit_code == 1
    assert "Traceback" not in result.output, result.output
    assert "✗" in result.output
    assert str(missing) in result.output


def test_init_routes_a_missing_target_too(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The fourth boundary, and the one that was missed the first time.

    ``init`` writes config through ``lifecycle``, not through the CLI modules
    the other three share, so wrapping those three left it printing 49 lines
    while its siblings printed one. Measuring all four rather than assuming a
    shared path is what surfaced it.
    """
    from aisquare.services import lifecycle as lifecycle_service

    missing = tmp_path / "dotfiles"
    monkeypatch.setattr(
        lifecycle_service,
        "save_config",
        lambda *_a, **_k: (_ for _ in ()).throw(_missing_target_error(missing)),
    )
    monkeypatch.setattr(paths, "config_path", lambda: tmp_path / "nope.toml")

    result = runner.invoke(app, ["init", "--yes"])

    assert result.exit_code == 1
    assert "Traceback" not in result.output, result.output
    assert "✗" in result.output
    assert str(missing) in result.output


def test_a_read_only_filesystem_reports_the_convention(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """EROFS is the operator's configuration choice, so it gets the one-liner.

    Same test as permission and missing-directory: did WHERE THEY POINTED THE
    CONFIG cause this, and can a line name the fix? Yes to both.
    """
    from aisquare.services import settings as settings_service

    def _boom(*_a: object, **_k: object) -> None:
        raise OSError(errno.EROFS, "Read-only file system", str(tmp_path / "config.toml"))

    monkeypatch.setattr(settings_service, "save_config", _boom)

    result = runner.invoke(app, ["config", "set", "explainability.proxy_url", "http://x"])

    assert result.exit_code == 1
    assert "Traceback" not in result.output, result.output
    assert "✗" in result.output
    assert str(tmp_path) in result.output


def test_a_full_disk_still_raises_with_its_traceback(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENOSPC is the machine breaking under a correct choice, so it stays loud.

    The discriminator is not a list of errnos, it is whether the operator's own
    configuration caused it. A full disk is probably breaking other things too;
    a tidy ✗ would understate that.
    """
    from aisquare.services import settings as settings_service

    def _boom(*_a: object, **_k: object) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(settings_service, "save_config", _boom)

    result = runner.invoke(app, ["config", "set", "explainability.proxy_url", "http://x"])

    assert isinstance(result.exception, OSError)
    assert result.exception.errno == errno.ENOSPC
    assert "✗" not in result.output


def test_the_redaction_command_translates_too(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The boundary the AST guard found that three sessions had missed by hand."""
    from aisquare.services import settings as settings_service

    missing = tmp_path / "dotfiles"
    monkeypatch.setattr(
        settings_service,
        "save_config",
        lambda *_a, **_k: (_ for _ in ()).throw(_missing_target_error(missing)),
    )

    result = runner.invoke(app, ["config", "redaction", "strict"])

    assert result.exit_code == 1
    assert "Traceback" not in result.output, result.output
    assert "✗" in result.output


# --- AISQUARE_HOME pointing at a file: the same convention, a wider reach ---------------
#
# ``ensure_home`` cannot mkdir over a regular file, and ``core.store.open_store``
# calls it unconditionally — so this failure is not confined to the four commands
# that write config. Measured with ``AISQUARE_HOME=<a regular file>``:
# ``asq --json init <dir>`` printed one refusal line, while ``asq --json fleet ls``
# printed 97 lines of Rich traceback with an EMPTY stdout (``project list``: 83,
# ``status``: 86). A traceback under ``--json`` breaks the machine contract, and
# the fleet UI's onboarding reads that stdout to say why an onboard failed.
#
# The reach is widened by a second guard, not by tidying every OSError in
# ``main()``: ``expected_home_creation_errors`` translates only the two exceptions
# a non-directory in the path produces, and only while ``home_blocker`` confirms
# the operator's home really is blocked. Everything else keeps its traceback.


def _blocked_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """``AISQUARE_HOME`` pointing AT a regular file."""
    blocked = tmp_path / "homefile"
    blocked.write_text("x", encoding="utf-8")
    monkeypatch.setenv(paths.HOME_ENV_VAR, str(blocked))
    return blocked


def test_init_reports_a_file_home_as_one_json_line(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reach the branch already had, and which nothing pinned."""
    blocked = _blocked_home(tmp_path, monkeypatch)
    project = tmp_path / "proj"
    project.mkdir()

    result = runner.invoke(app, ["--json", "init", str(project)])

    assert result.exit_code == 1, result.output
    assert "Traceback" not in result.output, result.output
    payload = json.loads(result.stdout)
    assert payload["error"] == "home_not_creatable", payload
    assert "AISQUARE_HOME" in payload["hint"]
    assert blocked.read_text(encoding="utf-8") == "x"


@pytest.mark.parametrize(
    "raised",
    [FileExistsError(errno.EEXIST, "File exists"), NotADirectoryError(errno.ENOTDIR, "Not a dir")],
    ids=["file-exists", "not-a-directory"],
)
def test_the_home_guard_translates_the_failure_from_any_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    raised: OSError,
) -> None:
    """One positive case per exception shape ``mkdir``/``open`` raises for a
    non-directory in the path, from the guard's own reach rather than from a
    command that happens to write config.

    Asserted on the ``--json`` payload, which is the artefact the machine contract
    is about: one object, exit 1, and the actionable sentence inside it.
    """
    blocked = _blocked_home(tmp_path, monkeypatch)
    set_state(RuntimeState(json_output=True))

    with pytest.raises(typer.Exit) as exit_info, common.expected_home_creation_errors():
        raise raised

    assert exit_info.value.exit_code == 1
    printed = capsys.readouterr().out
    assert printed.count("\n") == 1, printed
    payload = json.loads(printed)
    assert payload["error"] == "home_not_creatable", payload
    assert str(blocked) in payload["hint"] or "AISQUARE_HOME" in payload["hint"]


def test_the_home_guard_re_raises_when_the_home_is_healthy(
    home_is_a_directory: Path, tmp_path: Path
) -> None:
    """The negative control, and the whole reason this can sit around every command.

    ``CONTRIBUTING`` is explicit that an unexpected OSError is a bug and a
    traceback is the correct output for one. Without this half, the cheapest way
    to pass the two tests above is a handler that tidies every
    ``FileExistsError`` in the tree — including the ones that are defects.
    """
    with pytest.raises(FileExistsError), common.expected_home_creation_errors():
        raise FileExistsError(errno.EEXIST, "File exists", str(tmp_path / "unrelated"))


@pytest.fixture
def home_is_a_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The ordinary machine: AISQUARE_HOME is a directory that exists."""
    real = tmp_path / "real-home"
    real.mkdir()
    monkeypatch.setenv(paths.HOME_ENV_VAR, str(real))
    return real


def test_home_blocker_names_the_file_in_the_way_and_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Four shapes, because the message names the path the operator has to move.

    The dangling symlink is the one that is easy to miss: ``exists()`` follows the
    link and says no, but ``mkdir`` still refuses with ``FileExistsError``.
    """
    blocker = tmp_path / "in-the-way"
    blocker.write_text("", encoding="utf-8")

    monkeypatch.setenv(paths.HOME_ENV_VAR, str(blocker))
    assert common.home_blocker() == blocker

    monkeypatch.setenv(paths.HOME_ENV_VAR, str(blocker / "nested" / "home"))
    assert common.home_blocker() == blocker, "a file ABOVE the home blocks it too"

    dangling = tmp_path / "dangling"
    dangling.symlink_to(tmp_path / "nowhere")
    monkeypatch.setenv(paths.HOME_ENV_VAR, str(dangling))
    assert common.home_blocker() == dangling

    # The negative controls: a directory that exists, and one that does not yet.
    fine = tmp_path / "fine"
    fine.mkdir()
    monkeypatch.setenv(paths.HOME_ENV_VAR, str(fine))
    assert common.home_blocker() is None
    monkeypatch.setenv(paths.HOME_ENV_VAR, str(fine / "not-created-yet"))
    assert common.home_blocker() is None
