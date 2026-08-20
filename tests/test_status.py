"""End-to-end CLI behaviour for status and doctor."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app


@pytest.fixture(autouse=True)
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


def _json(output: str) -> Any:
    return json.loads(output)


def test_status_reports_counts(runner: CliRunner) -> None:
    runner.invoke(app, ["context", "add", "a pref", "--user"])
    runner.invoke(app, ["context", "add", "a convention", "--project"])
    result = runner.invoke(app, ["--json", "status"])
    assert result.exit_code == 0, result.output
    report = _json(result.stdout)
    assert report["initialized"] is True
    assert report["user_entries"] == 1
    assert report["project_entries"] == 1


def test_status_human(runner: CliRunner) -> None:
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "aisquare:" in result.stdout
    assert "home:" in result.stdout


def test_doctor_passes_after_init(runner: CliRunner) -> None:
    runner.invoke(app, ["init", "--no-onboard"])
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output  # warnings don't fail doctor
    assert "✓ home" in result.stdout
    assert "✓ python" in result.stdout


def test_doctor_fresh_flags_missing_home(runner: CliRunner) -> None:
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1  # a hard failure
    assert "✗ home" in result.stdout
    assert "aisquare init" in result.stdout  # the fix hint


def test_doctor_reports_dependency_checks(runner: CliRunner) -> None:
    result = runner.invoke(app, ["doctor"])
    for name in ("python", "install", "repomix", "tiktoken", "claude-code", "snapshot"):
        assert name in result.stdout


def test_doctor_warns_when_repomix_missing(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner.invoke(app, ["init", "--no-onboard"])
    monkeypatch.setattr("aisquare.services.diagnostics.shutil.which", lambda _name: None)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0  # missing optional dep is a warning, not a failure
    assert "⚠ repomix" in result.stdout
    assert "npm install -g repomix" in result.stdout


def test_doctor_json_includes_status_and_fix(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--json", "doctor"])
    checks = {check["name"]: check for check in _json(result.stdout)}
    assert checks["home"]["status"] == "fail"  # fresh, no init
    assert "init" in checks["home"]["fix"]


def test_doctor_reports_the_ci_test_bed_as_off_by_default() -> None:
    """Off is ``ok``, not a warning: it is the intended state for everyone who
    has not been asked to run the experiment, and a permanent warning trains
    people to ignore the one line that matters."""
    from aisquare.models import CheckStatus
    from aisquare.services.diagnostics import doctor

    check = next(c for c in doctor() if c.name == "ci test bed")
    assert check.status is CheckStatus.ok
    assert "off" in check.detail


def test_doctor_warns_when_enabled_without_an_endpoint(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from aisquare.models import CheckStatus
    from aisquare.services import ci_client
    from aisquare.services.diagnostics import doctor

    monkeypatch.setenv(ci_client.ENABLED_ENV_VAR, "1")
    monkeypatch.delenv(ci_client.URL_ENV_VAR, raising=False)
    check = next(c for c in doctor() if c.name == "ci test bed")
    assert check.status is CheckStatus.warn
    assert check.fix


def test_doctor_warns_when_the_endpoint_is_unreachable(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from aisquare.models import CheckStatus
    from aisquare.services import ci_client
    from aisquare.services.diagnostics import doctor

    monkeypatch.setenv(ci_client.ENABLED_ENV_VAR, "1")
    monkeypatch.setenv(ci_client.URL_ENV_VAR, "http://127.0.0.1:1")
    monkeypatch.setenv(ci_client.KEY_ENV_VAR, "k")
    check = next(c for c in doctor() if c.name == "ci test bed")
    assert check.status is CheckStatus.warn
    assert "prompts still work" in check.detail


def test_doctor_never_hangs_on_a_black_hole_endpoint(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A doctor that hangs is worse than one that reports nothing."""
    import time

    from aisquare.services import ci_client
    from aisquare.services.diagnostics import doctor

    monkeypatch.setenv(ci_client.ENABLED_ENV_VAR, "1")
    monkeypatch.setenv(ci_client.URL_ENV_VAR, "http://10.255.255.1:9")
    monkeypatch.setenv(ci_client.KEY_ENV_VAR, "k")
    started = time.monotonic()
    doctor()
    assert time.monotonic() - started < 10
