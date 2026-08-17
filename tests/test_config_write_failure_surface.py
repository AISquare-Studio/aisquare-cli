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
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import paths
from aisquare.core.config import AppConfig, save_config


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
