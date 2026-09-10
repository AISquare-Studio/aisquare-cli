"""The signed-in developer's view: ``doctor``'s identity lines and ``ci bind-workspace``.

C2a and C3 of ``docs/ci-user-identity-handoff.md``. A developer who ran
``aisquare login`` has no ``AISQUARE_CI_KEY`` and no ``AISQUARE_CI_RUN``; the run
comes from ``GET /v1/me`` for the workspace the project is bound to. These tests
drive that path through the stub server with a token from the environment, so
no credentials file is read and nothing here depends on a real sign-in.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import paths
from aisquare.core.config import AppConfig, load_config, save_config
from aisquare.models import CheckStatus, DoctorCheck
from aisquare.services import ci_client
from aisquare.services.diagnostics import doctor
from tests.ci_schemas import fixture
from tests.stub_ci_server import StubCI, serve

TOKEN = "aisq_test-token-0000000000000000000000000000"
ME = fixture("me.v1.valid")
PRINCIPAL = ME["principal_id"]
TEAM = "ws_kernel01"
QUIET = "ws_9a8b7c6d5e4f30211203948576abcdef"
"""The fixture's second workspace: a membership with no run published."""


@pytest.fixture
def stub() -> Iterator[StubCI]:
    yield from serve()


def signed_in(monkeypatch: pytest.MonkeyPatch, stub: StubCI) -> None:
    """On, pointed at the stub, signed in through the environment, nothing exported."""
    monkeypatch.setenv(ci_client.ENABLED_ENV_VAR, "1")
    monkeypatch.setenv(ci_client.URL_ENV_VAR, stub.url)
    monkeypatch.delenv(ci_client.KEY_ENV_VAR, raising=False)
    monkeypatch.delenv(ci_client.RUN_ENV_VAR, raising=False)
    monkeypatch.setenv("AISQUARE_TOKEN", TOKEN)


def bind(workspace: str) -> None:
    config = AppConfig()
    config.experiment.enabled = True
    config.experiment.workspace = workspace
    save_config(config)
    ci_client.reset_cache()


def ci_checks() -> dict[str, DoctorCheck]:
    return {c.name: c for c in doctor() if c.name.startswith("ci ")}


def one_workspace_body() -> str:
    only = json.loads(json.dumps(ME))
    only["workspaces"] = only["workspaces"][:1]
    return json.dumps(only)


# --- doctor -------------------------------------------------------------------


def test_signed_in_and_bound_is_five_green_lines(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    """The acceptance: no key, no run exported, and every line is green because
    the run came from GET /v1/me for the bound workspace."""
    signed_in(monkeypatch, stub)
    bind(TEAM)

    checks = ci_checks()

    assert {name: c.status for name, c in checks.items()} == {
        "ci test bed": CheckStatus.ok,
        "ci identity": CheckStatus.ok,
        "ci workspace": CheckStatus.ok,
        "ci endpoint": CheckStatus.ok,
        "ci descriptor": CheckStatus.ok,
    }
    assert "run_kernel0001 from GET /v1/me" in checks["ci test bed"].detail
    assert PRINCIPAL in checks["ci identity"].detail
    assert "2 workspaces" in checks["ci identity"].detail
    assert checks["ci workspace"].detail == f"{TEAM} (developer), run run_kernel0001"
    assert stub.me_fetches == 1


def test_the_identity_lines_never_carry_the_token(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    signed_in(monkeypatch, stub)
    bind(TEAM)

    for check in doctor():
        assert TOKEN not in check.detail
        assert TOKEN not in (check.fix or "")


def test_several_workspaces_and_no_binding_names_the_choice(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    """Guessing would bind the project to whichever the server listed first."""
    signed_in(monkeypatch, stub)

    checks = ci_checks()

    assert checks["ci identity"].status is CheckStatus.ok
    workspace = checks["ci workspace"]
    assert workspace.status is CheckStatus.warn
    assert TEAM in workspace.detail and QUIET in workspace.detail
    assert workspace.fix and "aisquare ci bind-workspace" in workspace.fix
    bed = checks["ci test bed"]
    assert bed.status is CheckStatus.warn and "no_run" in bed.detail
    assert "ci descriptor" not in checks, "no run, so nothing to fetch a descriptor for"


def test_a_single_workspace_needs_no_binding(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    signed_in(monkeypatch, stub)
    stub.me_body = one_workspace_body()

    checks = ci_checks()

    assert checks["ci workspace"].status is CheckStatus.ok
    assert "1 workspace —" not in checks["ci identity"].detail
    assert "in 1 workspace" in checks["ci identity"].detail


def test_a_binding_to_a_workspace_the_user_is_not_in_warns(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    signed_in(monkeypatch, stub)
    bind("ws_somebody_elses")

    workspace = ci_checks()["ci workspace"]

    assert workspace.status is CheckStatus.warn
    assert "not a member of ws_somebody_elses" in workspace.detail
    assert workspace.fix and "bind-workspace" in workspace.fix


def test_a_bound_workspace_with_no_run_says_so(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    signed_in(monkeypatch, stub)
    bind(QUIET)

    workspace = ci_checks()["ci workspace"]

    assert workspace.status is CheckStatus.warn
    assert "no run published" in workspace.detail
    assert workspace.fix and "controller" in workspace.fix


def test_a_401_on_me_says_to_sign_in_again(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    signed_in(monkeypatch, stub)
    stub.me_status = 401

    checks = ci_checks()

    identity = checks["ci identity"]
    assert identity.status is CheckStatus.warn
    assert "401" in identity.detail
    assert identity.fix and "aisquare login" in identity.fix
    assert "ci workspace" not in checks


def test_a_server_that_does_not_answer_me_is_named_not_blamed_on_the_session(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    signed_in(monkeypatch, stub)
    stub.me_status = 503

    identity = ci_checks()["ci identity"]

    assert identity.status is CheckStatus.warn
    assert "503" in identity.detail
    assert identity.fix and "aisquare login" not in identity.fix


def test_an_exported_run_still_wins_and_the_identity_is_still_shown(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    signed_in(monkeypatch, stub)
    monkeypatch.setenv(ci_client.RUN_ENV_VAR, "run_exported0001")

    checks = ci_checks()

    assert "run run_exported0001," in checks["ci test bed"].detail
    assert "GET /v1/me" not in checks["ci test bed"].detail
    assert checks["ci identity"].status is CheckStatus.ok


def test_the_experiment_token_path_shows_no_identity_lines(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    signed_in(monkeypatch, stub)
    monkeypatch.setenv(ci_client.KEY_ENV_VAR, "k")
    monkeypatch.setenv(ci_client.RUN_ENV_VAR, "run_kernel0001")

    checks = ci_checks()

    assert "ci identity" not in checks and "ci workspace" not in checks
    assert stub.me_fetches == 0


def test_doctor_probes_me_without_caching_it(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    signed_in(monkeypatch, stub)
    bind(TEAM)

    doctor()

    assert not paths.ci_cache_dir().exists()


# --- ci bind-workspace ---------------------------------------------------------


def _run(runner: CliRunner, *args: str) -> Result:
    return runner.invoke(app, ["ci", "bind-workspace", *args], catch_exceptions=False)


def _text(result: Result) -> str:
    """stdout and stderr together; ``fail`` prints its message to stderr."""
    try:
        return str(result.output) + str(result.stderr)
    except ValueError:
        return str(result.output)


def test_binding_a_workspace_you_are_in_is_written_to_config(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path, runner: CliRunner
) -> None:
    signed_in(monkeypatch, stub)

    result = _run(runner, TEAM)

    assert result.exit_code == 0, _text(result)
    assert f"bound this project to {TEAM} (developer), run run_kernel0001" in result.output
    assert load_config().experiment.workspace == TEAM
    assert stub.me_fetches == 1


def test_the_only_workspace_is_bound_without_being_named(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path, runner: CliRunner
) -> None:
    signed_in(monkeypatch, stub)
    stub.me_body = one_workspace_body()

    result = _run(runner)

    assert result.exit_code == 0, _text(result)
    assert load_config().experiment.workspace == TEAM


def test_several_workspaces_and_no_argument_lists_them_and_refuses(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path, runner: CliRunner
) -> None:
    signed_in(monkeypatch, stub)

    result = _run(runner)

    assert result.exit_code == 1
    text = _text(result)
    assert TEAM in text and QUIET in text
    assert load_config().experiment.workspace == ""


def test_a_workspace_you_are_not_in_is_refused(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path, runner: CliRunner
) -> None:
    """The binding is a selector and the server would refuse anyway; refusing
    here saves the developer a session of no_run rows."""
    signed_in(monkeypatch, stub)

    result = _run(runner, "ws_somebody_elses")

    assert result.exit_code == 1
    assert "not one of your workspaces" in _text(result)
    assert load_config().experiment.workspace == ""


def test_clear_forgets_the_binding(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path, runner: CliRunner
) -> None:
    signed_in(monkeypatch, stub)
    bind(TEAM)

    result = _run(runner, "--clear")

    assert result.exit_code == 0, _text(result)
    assert load_config().experiment.workspace == ""
    assert stub.me_fetches == 0, "clearing asks the server nothing"


def test_a_rejected_token_points_at_login(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path, runner: CliRunner
) -> None:
    signed_in(monkeypatch, stub)
    stub.me_status = 401

    result = _run(runner, TEAM)

    assert result.exit_code == 1
    assert "aisquare login" in _text(result)


def test_off_and_no_bearer_each_refuse_by_name(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path, runner: CliRunner
) -> None:
    monkeypatch.delenv(ci_client.ENABLED_ENV_VAR, raising=False)
    assert "AISQUARE_CI=1" in _text(_run(runner, TEAM))

    signed_in(monkeypatch, stub)
    monkeypatch.delenv("AISQUARE_TOKEN", raising=False)
    assert "aisquare login" in _text(_run(runner, TEAM))
    assert stub.me_fetches == 0


def test_the_command_leaves_no_me_cache_behind(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path, runner: CliRunner
) -> None:
    signed_in(monkeypatch, stub)

    _run(runner, TEAM)

    assert not paths.ci_cache_dir().exists()
