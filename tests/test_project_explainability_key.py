"""A workspace key per project (#141): stored, resolved first, bound to one deployment.

Every claim has its control: the project key wins over the machine key for ITS
project and deployment only; a binding for another deployment is not a key for
this one (the rule ``test_key_never_crosses_deployments.py`` pins for the file,
applied to the project); the value never reaches stdout; the launch's proxy
header carries the project's key; `doctor` still opens no store.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import paths
from aisquare.core.config import AppConfig, ExplainabilityTarget, save_config
from aisquare.core.store import store_session
from aisquare.core.workspace import pin_project, project_id_for
from aisquare.models import ProjectInfo
from aisquare.services import explainability as service
from aisquare.services import explainability_ops as ops

PROJECT_KEY = "pk-live-0123456789abcdef"
MACHINE_KEY = "mk-machine-fedcba9876543210"


def _settings(*, default_var: bool = True) -> AppConfig:
    config = AppConfig()
    config.explainability.enabled = True
    config.explainability.target = "stg"
    config.explainability.targets = {
        "stg": ExplainabilityTarget(
            gateway_url="https://stg.example", proxy_url="http://127.0.0.1:9199"
        ),
        "prod": ExplainabilityTarget(
            gateway_url="https://prod.example",
            proxy_url="http://127.0.0.1:9199",
            **({} if default_var else {"api_key_env": "PROD_KEY"}),
        ),
    }
    save_config(config)
    return config


def _project(root: Path, *, pin: bool = True) -> ProjectInfo:
    root.mkdir(parents=True, exist_ok=True)
    info = ProjectInfo(id=project_id_for(root.resolve()), root=root.resolve(), linked_repos=[])
    with store_session() as store:
        store.onboard_project(info)
    if pin:
        pin_project(info.id)
    return info


@pytest.fixture
def home(isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    paths.ensure_home()
    monkeypatch.delenv("EXPLAINABILITY_API_KEY", raising=False)
    monkeypatch.delenv("PROD_KEY", raising=False)
    return isolated_home


# --- the store and the file -------------------------------------------------------------------


def test_the_binding_round_trips_and_the_value_lives_in_a_600_file_not_the_store(
    home: Path, tmp_path: Path
) -> None:
    project = _project(tmp_path / "api")
    path = service.store_project_api_key(project.id, f"  {PROJECT_KEY}\n")
    assert path == service.project_key_path(project.id)
    assert (
        path.read_text(encoding="utf-8") == PROJECT_KEY and (path.stat().st_mode & 0o777) == 0o600
    )
    with store_session() as store:
        assert store.project_explainability(project.id) is None
        binding = store.set_project_explainability(
            project.id, target="stg", key_path=path, set_by="me@example.com"
        )
        assert (
            binding.target == "stg"
            and binding.key_path == path
            and binding.set_by == "me@example.com"
        )
        assert [b.project_id for b in store.project_explainability_all()] == [project.id]
        # re-pointing keeps one row per project
        moved = store.set_project_explainability(
            project.id, target="prod", key_path=path, set_by=None
        )
        assert moved.target == "prod" and len(store.project_explainability_all()) == 1
        assert store.clear_project_explainability(project.id) is True
        assert store.clear_project_explainability(project.id) is False
    assert PROJECT_KEY not in paths.db_path().read_bytes().decode("latin-1"), "never in context.db"
    assert service.clear_project_api_key(project.id) is True and not path.exists()
    assert service.clear_project_api_key(project.id) is False


# --- the resolver ------------------------------------------------------------------------------


def test_the_project_key_wins_for_its_project_and_deployment_only(
    home: Path, tmp_path: Path
) -> None:
    settings = _settings().explainability
    service.store_api_key(MACHINE_KEY)
    api = _project(tmp_path / "api")
    other = _project(tmp_path / "other", pin=False)
    path = service.store_project_api_key(api.id, PROJECT_KEY)
    with store_session() as store:
        store.set_project_explainability(api.id, target="stg", key_path=path, set_by=None)

    mine = ops.resolve_target(settings, project_id=api.id)
    assert (mine.api_key, mine.key_source, mine.project_id) == (PROJECT_KEY, "project", api.id)
    assert "the project's own key" in mine.key_origin and str(path) in mine.key_origin
    theirs = ops.resolve_target(settings, project_id=other.id)
    assert (theirs.api_key, theirs.key_source) == (
        MACHINE_KEY,
        "file",
    )  # another project: the machine
    nobody = ops.resolve_target(settings)
    assert (nobody.api_key, nobody.key_source) == (MACHINE_KEY, "file")  # no project: as before
    # Bound to stg: a prod resolve for the same project never sees it.
    prod = ops.resolve_target(settings, "prod", project_id=api.id)
    assert prod.key_source != "project" and prod.api_key != PROJECT_KEY
    # The target's variable still outranks the machine file, and the project key both.
    os.environ["EXPLAINABILITY_API_KEY"] = "env-key"
    try:
        assert ops.resolve_target(settings, project_id=other.id).key_source == "env"
        assert ops.resolve_target(settings, project_id=api.id).key_source == "project"
    finally:
        del os.environ["EXPLAINABILITY_API_KEY"]
    # A binding whose file is gone reads as no project key: the next rung answers.
    path.unlink()
    assert ops.resolve_target(settings, project_id=api.id).key_source == "file"


# --- the CLI -----------------------------------------------------------------------------------


def test_key_set_show_clear_never_print_the_value(
    home: Path, tmp_path: Path, runner: CliRunner
) -> None:
    _settings()
    api = _project(tmp_path / "api")

    refused = runner.invoke(app, ["explainability", "key", "set", PROJECT_KEY])
    assert refused.exit_code != 0, "argv is never a key"

    result = runner.invoke(app, ["explainability", "key", "set"], input=PROJECT_KEY + "\n")
    assert result.exit_code == 0, result.output
    assert "✓ key attached to api for target stg" in result.stdout and "(mode 600)" in result.stdout
    assert PROJECT_KEY not in result.stdout
    path = service.project_key_path(api.id)
    assert path.read_text(encoding="utf-8") == PROJECT_KEY

    shown = runner.invoke(app, ["--json", "explainability", "key", "show"])
    assert shown.exit_code == 0, shown.output
    payload = json.loads(shown.stdout)
    assert (
        payload["attached"] is True
        and payload["target"] == "stg"
        and payload["key_source"] == "project"
    )
    assert payload["key_set"] is True and PROJECT_KEY not in shown.stdout
    plain = runner.invoke(app, ["explainability", "key", "show"])
    assert "project key for target stg" in plain.stdout and PROJECT_KEY not in plain.stdout

    status = runner.invoke(app, ["--json", "explainability", "status"])
    assert json.loads(status.stdout)["key_source"] == "project"

    os.environ["NEW_KEY"] = "pk-rotated"
    try:
        rotated = runner.invoke(
            app, ["explainability", "key", "set", "--from-env", "NEW_KEY", "--target", "prod"]
        )
    finally:
        del os.environ["NEW_KEY"]
    assert rotated.exit_code == 0, rotated.output
    assert path.read_text(encoding="utf-8") == "pk-rotated"
    for_stg = runner.invoke(app, ["explainability", "key", "show"])
    assert "not used for target stg" in for_stg.stdout  # bound to prod now

    cleared = runner.invoke(app, ["explainability", "key", "clear"])
    assert cleared.exit_code == 0 and "✓ key cleared for api" in cleared.stdout
    assert not path.exists()
    again = runner.invoke(app, ["--json", "explainability", "key", "clear"])
    assert json.loads(again.stdout)["cleared"] is False
    missing = runner.invoke(app, ["explainability", "key", "set", "--from-env", "UNSET_VAR_X"])
    assert missing.exit_code != 0


# --- the lane that matters: the proxy header of a launch --------------------------------------


def test_the_launch_wiring_carries_the_projects_key(home: Path, tmp_path: Path) -> None:
    settings = _settings().explainability
    api = _project(tmp_path / "api")
    path = service.store_project_api_key(api.id, PROJECT_KEY)
    with store_session() as store:
        store.set_project_explainability(api.id, target="stg", key_path=path, set_by=None)
    resolved = ops.resolve_target(settings, project_id=api.id)
    wiring = service.wire_session(
        settings,
        "coder",
        session_id="ses-1",
        api_key=resolved.api_key,
        prober=lambda url: service.ProxyProbe(True, "proxy healthy"),
        post_root=False,
    )
    assert wiring.traced, wiring.reason
    assert f"X-AISquare-Key: {PROJECT_KEY}" in wiring.env["ANTHROPIC_CUSTOM_HEADERS"]


def test_doctor_still_opens_no_store_to_resolve_a_key(isolated_home: Path) -> None:
    from aisquare.services import diagnostics

    assert not isolated_home.exists()
    diagnostics.doctor()
    assert not isolated_home.exists(), "a machine-level resolve must not create the home"
