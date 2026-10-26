"""``aisquare explainability doctor``: the section ``aisquare doctor`` used to carry.

Explainability is dormant (unmaintained, unused), so ``aisquare doctor`` has no row of its
own for it and no ``--target``/``--project``, and ``--fix`` there only makes the home. The
section lives on under its own command group, doing what it did: the same
``explainability_ops.checks`` and ``apply_fixes``, ``--json`` as the others, and exit 1 on a
failed row. A move, not new behaviour: the checks' own tests are test_explainability_ops.py's.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import paths
from aisquare.core.config import load_config
from aisquare.models import CheckStatus, DoctorCheck
from aisquare.services import explainability_ops as ops


def _row(name: str, status: CheckStatus, fix: str | None = None) -> DoctorCheck:
    return DoctorCheck(name=name, status=status, detail=f"{name} detail", fix=fix)


@pytest.fixture
def seen(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """``ops.checks`` and ``ops.apply_fixes`` replaced by recorders, so what the command
    passes them is what is read."""
    calls: dict[str, Any] = {
        "checks": [],
        "fixes": [],
        "rows": [_row("explainability", CheckStatus.ok)],
    }

    def checks(**kwargs: Any) -> list[DoctorCheck]:
        calls["checks"].append(kwargs)
        rows: list[DoctorCheck] = calls["rows"]
        return rows

    def apply_fixes(**kwargs: Any) -> list[str]:
        calls["fixes"].append({key: value for key, value in kwargs.items() if key != "confirm"})
        return ["did a thing"]

    monkeypatch.setattr(ops, "checks", checks)
    monkeypatch.setattr(ops, "apply_fixes", apply_fixes)
    return calls


def test_it_prints_the_sections_rows_and_exits_zero_while_nothing_fails(
    runner: CliRunner, seen: dict[str, Any]
) -> None:
    seen["rows"] = [
        _row("explainability", CheckStatus.ok),
        _row("explainability-sdk", CheckStatus.warn, "Install it: pip install x"),
    ]

    result = runner.invoke(app, ["explainability", "doctor"])

    assert result.exit_code == 0, result.output
    assert "✓ explainability: explainability detail" in result.output
    assert "⚠ explainability-sdk: explainability-sdk detail" in result.output
    assert "→ Install it: pip install x" in result.output
    assert seen["checks"] == [{"live": False, "target_name": None, "project_id": None}]
    assert seen["fixes"] == []


def test_a_failed_row_is_exit_one_as_it_was_in_doctor(
    runner: CliRunner, seen: dict[str, Any]
) -> None:
    seen["rows"] = [_row("explainability", CheckStatus.ok), _row("proxy", CheckStatus.fail, "x")]

    text = runner.invoke(app, ["explainability", "doctor"])
    as_json = runner.invoke(app, ["--json", "explainability", "doctor"])

    assert text.exit_code == as_json.exit_code == 1
    assert "✗ proxy: proxy detail" in text.output
    assert [row["name"] for row in json.loads(as_json.stdout)] == ["explainability", "proxy"]


def test_live_target_and_project_reach_the_checks(
    runner: CliRunner, seen: dict[str, Any], tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aisquare.core.store import store_session
    from aisquare.models import ProjectInfo

    root = tmp_path / "work"
    root.mkdir()
    with store_session() as store:
        project = store.onboard_project(ProjectInfo(id="prj_doc", root=root, name="doc"))

    result = runner.invoke(
        app, ["explainability", "doctor", "--live", "--target", "stg", "-P", project.id]
    )

    assert result.exit_code == 0, result.output
    assert seen["checks"] == [{"live": True, "target_name": "stg", "project_id": project.id}]


def test_an_unknown_project_is_a_usage_error_not_a_failed_check(
    runner: CliRunner, seen: dict[str, Any]
) -> None:
    result = runner.invoke(app, ["explainability", "doctor", "--project", "nope"])

    assert result.exit_code == 2, result.output
    assert seen["checks"] == []


def test_fix_runs_the_repairs_before_the_re_check(runner: CliRunner, seen: dict[str, Any]) -> None:
    result = runner.invoke(app, ["explainability", "doctor", "--fix", "--target", "prod", "--yes"])

    assert result.exit_code == 0, result.output
    assert result.output.index("fix: did a thing") < result.output.index("explainability detail")
    assert seen["fixes"] == [{"target": "prod", "assume_yes": True}]
    assert seen["checks"][0]["target_name"] == "prod"


def test_without_the_stubs_it_is_the_same_section_the_ops_return(runner: CliRunner) -> None:
    """The real thing on an untouched machine: one line, the switch, which says how to turn
    tracing on, and that line names this command's group, not ``aisquare doctor``."""
    result = runner.invoke(app, ["--json", "explainability", "doctor"])

    rows = json.loads(result.stdout)
    assert result.exit_code == 0, result.output
    assert rows == [check.model_dump(mode="json") for check in ops.checks()]
    assert [row["name"] for row in rows] == ["explainability"]
    assert "aisquare explainability enable" in rows[0]["detail"]


def test_the_group_lists_it(runner: CliRunner) -> None:
    helped = runner.invoke(app, ["explainability", "--help"]).output

    assert "doctor" in helped


# --- what `aisquare doctor --fix` keeps ---------------------------------------------------


def test_doctor_fix_makes_the_home_and_touches_nothing_of_explainability(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It turned tracing on for a ``--target`` and offered to pip-install the SDK. Neither
    is its business any more: the home is all it makes."""

    def refused(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("`aisquare doctor --fix` reached explainability")

    monkeypatch.setattr(ops, "apply_fixes", refused)
    monkeypatch.setattr(ops, "install_sdk", refused)
    assert not paths.aisquare_home().exists()

    result = runner.invoke(app, ["doctor", "--fix", "--yes"], catch_exceptions=False)

    assert f"fix: created the aisquare home at {paths.aisquare_home()}" in result.output
    assert paths.aisquare_home().exists()
    assert load_config().explainability.enabled is False


def test_doctor_fix_says_nothing_when_there_is_nothing_to_make(runner: CliRunner) -> None:
    runner.invoke(app, ["init", "--no-onboard"], catch_exceptions=False)

    result = runner.invoke(app, ["doctor", "--fix"], catch_exceptions=False)

    assert "fix:" not in result.output
