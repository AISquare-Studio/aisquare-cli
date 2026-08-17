"""``aisquare launch <role>`` — the ergonomic replacement for ``AISQUARE_ROLE=… claude``."""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli import launch as launch_cli
from aisquare.cli.app import app
from aisquare.core.orchestrator import team_project
from aisquare.core.store import store_session


@pytest.fixture
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Intercept the exec so the agent is never really launched."""
    captured: dict[str, Any] = {}

    def fake_exec(binary: str, argv: list[str], env: dict[str, str]) -> None:
        captured.update(binary=binary, argv=argv, env=env)

    monkeypatch.setattr(launch_cli, "_exec", fake_exec)
    monkeypatch.setattr(shutil, "which", lambda cmd: f"/usr/local/bin/{cmd}")
    return captured


def test_launch_execs_the_agent_with_the_role_in_env(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any]
) -> None:
    result = runner.invoke(app, ["launch", "coder"])

    assert result.exit_code == 0, result.output
    assert spy["binary"] == "/usr/local/bin/claude"
    assert spy["argv"] == ["claude"]
    assert spy["env"]["AISQUARE_ROLE"] == "coder"


def test_launch_forwards_extra_arguments_to_the_agent(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any]
) -> None:
    result = runner.invoke(app, ["launch", "runner", "--model", "opus", "-p", "go"])

    assert result.exit_code == 0, result.output
    # The role is consumed; everything after it reaches the agent untouched.
    assert spy["argv"] == ["claude", "--model", "opus", "-p", "go"]
    assert spy["env"]["AISQUARE_ROLE"] == "runner"


def test_launch_activates_the_project(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any]
) -> None:
    # A role launch is the opt-in for the repo — the board must exist afterwards.
    with store_session() as store:
        assert not store.team_active(team_project(work_dir).id)

    assert runner.invoke(app, ["launch", "planner"]).exit_code == 0

    with store_session() as store:
        assert store.team_active(team_project(work_dir).id)


def test_launch_rejects_an_unknown_role(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any]
) -> None:
    result = runner.invoke(app, ["launch", "reviewer"])

    assert result.exit_code == 1
    assert "unknown role" in result.output
    assert not spy, "nothing should be exec'd for an invalid role"


def test_launch_reports_a_missing_agent_binary(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda cmd: None)

    result = runner.invoke(app, ["launch", "coder"])

    assert result.exit_code == 1
    assert "not on your PATH" in result.output


def test_launch_honours_a_custom_agent_command(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any]
) -> None:
    result = runner.invoke(app, ["launch", "coder", "--command", "claude-next"])

    assert result.exit_code == 0, result.output
    assert spy["argv"] == ["claude-next"]


def test_launch_env_flag_sets_a_variable_for_the_agent(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any], tmp_path: Path
) -> None:
    # Parallel installs are reached by setting the variables the shell alias
    # sets, which is why this takes a variable rather than an "account": the
    # CLI has no business knowing what any of these keys mean.
    account = tmp_path / ".claude-account1"
    account.mkdir()

    result = runner.invoke(app, ["launch", "coder", "--env", f"CLAUDE_CONFIG_DIR={account}"])

    assert result.exit_code == 0, result.output
    assert spy["env"]["CLAUDE_CONFIG_DIR"] == str(account)
    assert spy["env"]["AISQUARE_ROLE"] == "coder"


def test_launch_does_not_validate_the_values_it_carries(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any], tmp_path: Path
) -> None:
    # Deliberately permissive. Rejecting a non-existent directory would require
    # knowing WHICH keys name directories -- exactly the coupling this design
    # removes. The operator owns the values; we carry them.
    missing = tmp_path / "not-created"

    result = runner.invoke(app, ["launch", "coder", "--env", f"CLAUDE_CONFIG_DIR={missing}"])

    assert result.exit_code == 0, result.output
    assert spy["env"]["CLAUDE_CONFIG_DIR"] == str(missing)


def test_launch_without_a_profile_leaves_the_ambient_config_dir_alone(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/from/the/shell")

    assert runner.invoke(app, ["launch", "coder"]).exit_code == 0
    assert spy["env"]["CLAUDE_CONFIG_DIR"] == "/from/the/shell"


def test_launch_forwards_the_global_output_flags_to_the_agent(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any]
) -> None:
    """Anything after the role belongs to the AGENT — the five global flags too.

    The root group injects --json/--verbose/-v/--quiet/-q/--no-color/--profile
    into every command (issue #24). On an arg-forwarding command that would let
    click parse them OUT of the forwarded argv (--profile even eats its value),
    so the agent silently never sees flags the user typed for it. launch
    declares ignore_unknown_options — injection must respect that and skip it.
    """
    result = runner.invoke(app, ["launch", "coder", "--verbose", "--profile", "p1", "--json", "-q"])

    assert result.exit_code == 0, result.output
    assert spy["argv"] == ["claude", "--verbose", "--profile", "p1", "--json", "-q"]


def test_launch_respects_the_master_off_switch(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISQUARE_TEAM", "0")

    result = runner.invoke(app, ["launch", "coder"])

    assert result.exit_code == 1
    assert not spy, "the orchestrator is off — nothing should be launched"


# ── explainability wiring at the env seam ────────────────────────────────────


def _tracing_on(proxy_url: str) -> None:
    from aisquare.core.config import AppConfig, ExplainabilitySettings, save_config

    save_config(AppConfig(explainability=ExplainabilitySettings(enabled=True, proxy_url=proxy_url)))


def test_launch_default_config_adds_no_tracing(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tracing off (the default) must leave the launch byte-identical: no env
    delta, no extra output — zero cost to every existing user."""
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)

    result = runner.invoke(app, ["launch", "coder"])

    assert result.exit_code == 0, result.output
    assert "ANTHROPIC_BASE_URL" not in spy["env"]
    assert "ANTHROPIC_CUSTOM_HEADERS" not in spy["env"]
    assert "explainability" not in result.output


def test_launch_enabled_wires_the_traced_env(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from aisquare.services import explainability as explainability_service
    from aisquare.services.explainability import SessionWiring

    _tracing_on("http://127.0.0.1:9")

    def fake_wire(settings: Any, role: str, **kwargs: Any) -> SessionWiring:
        return SessionWiring(
            traced=True,
            reason=f"traced as aisquare-{role} (pipeline p-1)",
            env={
                "ANTHROPIC_BASE_URL": settings.proxy_url,
                "ANTHROPIC_CUSTOM_HEADERS": f"X-Agent-Name: aisquare-{role}\nX-Pipeline-Id: p-1",
            },
        )

    monkeypatch.setattr(explainability_service, "wire_session", fake_wire)

    result = runner.invoke(app, ["launch", "coder"])

    assert result.exit_code == 0, result.output
    assert spy["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:9"
    assert "X-Pipeline-Id" in spy["env"]["ANTHROPIC_CUSTOM_HEADERS"]
    assert "traced as aisquare-coder" in result.output


def test_launch_with_dead_proxy_still_launches(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail-open through the REAL probe: tracing enabled, nothing listening —
    the launch must proceed untraced instead of breaking or misrouting."""
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    _tracing_on("http://127.0.0.1:9")  # discard port — connection refused

    result = runner.invoke(app, ["launch", "coder"])

    assert result.exit_code == 0, result.output
    assert spy["binary"].endswith("/claude"), "the launch must happen regardless"
    assert "ANTHROPIC_BASE_URL" not in spy["env"]
    assert "untraced" in result.output


def test_launch_starts_the_agent_on_the_id_it_traces_under(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE correlation spine, through the real wiring: the id the agent is
    STARTED on is the id in ``X-Pipeline-Id``.

    The board keys a session by the id the agent reports to the SessionStart
    hook, so those two being equal is the only thing that lets a board row and
    a gateway Run be joined. Asserted end to end (real probe, real wiring)
    because it is a property of the chain, not of any one function in it.
    """
    from aisquare.services.explainability import join_records
    from tests.proxy_stub import healthy_proxy

    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    with healthy_proxy() as proxy_url:
        _tracing_on(proxy_url)
        result = runner.invoke(app, ["launch", "coder"])

    assert result.exit_code == 0, result.output
    argv = spy["argv"]
    assert "--session-id" in argv, "a traced launch must name the session it traces"
    started_on = argv[argv.index("--session-id") + 1]
    uuid.UUID(started_on)  # claude accepts nothing else
    assert f"X-Pipeline-Id: {started_on}" in spy["env"]["ANTHROPIC_CUSTOM_HEADERS"]

    # …and written down, so the join survives without dashboard access.
    (record,) = join_records()
    assert record["session_id"] == started_on
    assert record["pipeline_id"] == started_on
    assert record["agent_name"] == "aisquare-coder"
    assert record["joined"] is True
    assert record["role"] == "coder"


def test_two_launches_are_started_on_different_ids(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two agents on one session id merge into a single board row AND a single
    Run — the failure both halves of this design exist to prevent."""
    from tests.proxy_stub import healthy_proxy

    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    ids = []
    with healthy_proxy() as proxy_url:
        _tracing_on(proxy_url)
        for _ in range(2):
            assert runner.invoke(app, ["launch", "coder"]).exit_code == 0
            argv = spy["argv"]
            ids.append(argv[argv.index("--session-id") + 1])

    assert ids[0] != ids[1]


def test_launch_does_not_pin_an_id_it_cannot_own(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """--continue resumes a session chosen at run time, and a custom agent may
    not speak the flag at all. Both still trace — unjoined, with the reason —
    because a flag the agent rejects costs the launch, and nothing may."""
    from tests.proxy_stub import healthy_proxy

    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    with healthy_proxy() as proxy_url:
        _tracing_on(proxy_url)

        assert runner.invoke(app, ["launch", "coder", "--continue"]).exit_code == 0
        assert spy["argv"] == ["claude", "--continue"]
        assert "X-Pipeline-Id" in spy["env"]["ANTHROPIC_CUSTOM_HEADERS"]

        result = runner.invoke(app, ["launch", "coder", "--command", "aider"])
        assert result.exit_code == 0, result.output
        assert spy["argv"] == ["aider"]
        assert "not joined" in result.output


def test_untraced_launch_is_never_handed_a_session_id(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail-open means the fallback is the launch you would have got anyway —
    argv included. An id pinned onto an untraced launch buys no correlation
    and spends real risk."""
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    _tracing_on("http://127.0.0.1:9")  # discard port — connection refused

    result = runner.invoke(app, ["launch", "coder", "--model", "opus"])

    assert result.exit_code == 0, result.output
    assert spy["argv"] == ["claude", "--model", "opus"]
    assert "untraced" in result.output


def test_launch_survives_an_unwritable_join_log(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The join log is a convenience; the session it describes is the product."""
    from aisquare.core import paths
    from tests.proxy_stub import healthy_proxy

    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    paths.explainability_dir().parent.mkdir(parents=True, exist_ok=True)
    paths.explainability_dir().write_text("in the way", encoding="utf-8")

    with healthy_proxy() as proxy_url:
        _tracing_on(proxy_url)
        result = runner.invoke(app, ["launch", "coder"])

    assert result.exit_code == 0, result.output
    assert "--session-id" in spy["argv"], "the launch is traced and pinned regardless"
    assert "join record not written" in result.output


def test_launch_reuses_the_forwarded_session_id(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A --session-id the caller passes to the agent becomes the pipeline id —
    read from the forwarded args, never injected into them."""
    from aisquare.services import explainability as explainability_service
    from aisquare.services.explainability import SessionWiring

    _tracing_on("http://127.0.0.1:9")
    seen: dict[str, Any] = {}

    def fake_wire(settings: Any, role: str, **kwargs: Any) -> SessionWiring:
        seen.update(kwargs)
        return SessionWiring(traced=False, reason="stubbed")

    monkeypatch.setattr(explainability_service, "wire_session", fake_wire)

    runner.invoke(app, ["launch", "coder", "--session-id", "sess-7"])
    assert seen["session_id"] == "sess-7"

    runner.invoke(app, ["launch", "coder", "--session-id=sess-8"])
    assert seen["session_id"] == "sess-8"


def test_launch_survives_a_corrupt_config(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any]
) -> None:
    """launch never read the config before tracing arrived; a corrupt
    config.toml must not become a new way for a launch to die (the zero-
    breakage bar). Worst allowed outcome: untraced, with a reason."""
    from aisquare.core import paths

    paths.config_path().parent.mkdir(parents=True, exist_ok=True)
    paths.config_path().write_text("explainability = [unclosed", encoding="utf-8")

    result = runner.invoke(app, ["launch", "coder"])

    assert result.exit_code == 0, result.output
    assert spy["env"]["AISQUARE_ROLE"] == "coder", "the launch must happen regardless"
    assert "config unreadable" in result.output
